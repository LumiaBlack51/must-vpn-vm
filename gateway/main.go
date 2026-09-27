// MUST VM gateway. All guest egress uses an explicitly bound physical interface.
package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"strconv"
	"sync"
	"syscall"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/udp"
	"gvisor.dev/gvisor/pkg/waiter"
)

var guestMAC = []byte{0x52, 0x54, 0, 0x4d, 0x55, 0x53}
var gatewayMAC = []byte{0x52, 0x54, 0, 0x4d, 0x55, 1}
var gatewayIP = net.IPv4(10, 77, 0, 1).To4()

type uplink struct {
	name  string
	index int
	ip    net.IP
}

func newUplink(name, address string) (*uplink, error) {
	iface, err := net.InterfaceByName(name)
	if err != nil {
		return nil, err
	}
	ip := net.ParseIP(address).To4()
	if ip == nil {
		return nil, errors.New("IPv4 source address required")
	}
	u := &uplink{name, iface.Index, ip}
	if err = u.check(); err != nil {
		return nil, err
	}
	return u, nil
}
func (u *uplink) check() error {
	iface, err := net.InterfaceByName(u.name)
	if err != nil {
		return err
	}
	if iface.Index != u.index || iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
		return errors.New("selected uplink unavailable")
	}
	addrs, err := iface.Addrs()
	if err != nil {
		return err
	}
	for _, a := range addrs {
		ip, _, e := net.ParseCIDR(a.String())
		if e == nil && ip.Equal(u.ip) {
			return nil
		}
	}
	return errors.New("uplink address changed; restart with current address")
}
func (u *uplink) dial(ctx context.Context, network, address string) (net.Conn, error) {
	if err := u.check(); err != nil {
		return nil, err
	}
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}
	ip := net.ParseIP(host)
	if ip == nil || ip.To4() == nil || ip.IsLoopback() || ip.IsUnspecified() || ip.IsMulticast() || ip.IsLinkLocalUnicast() || ip.Equal(u.ip) {
		return nil, errors.New("prohibited destination")
	}
	d := net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}
	if network == "tcp4" {
		d.LocalAddr = &net.TCPAddr{IP: u.ip}
	} else {
		d.LocalAddr = &net.UDPAddr{IP: u.ip}
	}
	d.Control = func(_, _ string, c syscall.RawConn) error {
		var inner error
		err := c.Control(func(fd uintptr) { inner = bindInterface(fd, u.name, u.index) })
		if err != nil {
			return err
		}
		return inner
	}
	return d.DialContext(ctx, network, address)
}
func relay(a, b net.Conn) {
	defer a.Close()
	defer b.Close()
	done := make(chan struct{}, 2)
	copyOne := func(dst, src net.Conn) {
		io.Copy(dst, src)
		if cw, ok := dst.(interface{ CloseWrite() error }); ok {
			cw.CloseWrite()
		}
		done <- struct{}{}
	}
	go copyOne(a, b)
	go copyOne(b, a)
	<-done
	// Preserve half-close, but do not leak a connection on a hung peer.
	a.SetDeadline(time.Now().Add(30 * time.Second))
	b.SetDeadline(time.Now().Add(30 * time.Second))
	<-done
}
func destination(id stack.TransportEndpointID) string {
	return net.JoinHostPort(id.LocalAddress.String(), strconv.Itoa(int(id.LocalPort)))
}
func newStack(u *uplink) (*stack.Stack, *channel.Endpoint) {
	s := stack.New(stack.Options{NetworkProtocols: []stack.NetworkProtocolFactory{ipv4.NewProtocol}, TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol, udp.NewProtocol}})
	ep := channel.New(256, 1500, "")
	if e := s.CreateNIC(1, ep); e != nil {
		panic(e)
	}
	s.AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: tcpip.AddrFrom4Slice(gatewayIP).WithPrefix()}, stack.AddressProperties{})
	s.SetPromiscuousMode(1, true)
	s.SetSpoofing(1, true)
	s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
	slots := make(chan struct{}, 256)
	tf := tcp.NewForwarder(s, 0, 128, func(r *tcp.ForwarderRequest) {
		select {
		case slots <- struct{}{}:
		default:
			r.Complete(true)
			return
		}
		go func() {
			defer func() { <-slots }()
			out, err := u.dial(context.Background(), "tcp4", destination(r.ID()))
			if err != nil {
				r.Complete(true)
				return
			}
			var wq waiter.Queue
			endpoint, e := r.CreateEndpoint(&wq)
			if e != nil {
				out.Close()
				r.Complete(true)
				return
			}
			r.Complete(false)
			relay(gonet.NewTCPConn(&wq, endpoint), out)
		}()
	})
	s.SetTransportProtocolHandler(tcp.ProtocolNumber, tf.HandlePacket)
	uf := udp.NewForwarder(s, func(r *udp.ForwarderRequest) bool {
		select {
		case slots <- struct{}{}:
		default:
			return false
		}
		var wq waiter.Queue
		endpoint, e := r.CreateEndpoint(&wq)
		if e != nil {
			<-slots
			return false
		}
		in := gonet.NewUDPConn(&wq, endpoint)
		go func() {
			defer func() { <-slots }()
			defer in.Close()
			out, err := u.dial(context.Background(), "udp4", destination(r.ID()))
			if err != nil {
				return
			}
			defer out.Close()
			done := make(chan struct{}, 2)
			pump := func(dst, src net.Conn) {
				defer func() { done <- struct{}{} }()
				b := make([]byte, 65535)
				for {
					src.SetReadDeadline(time.Now().Add(60 * time.Second))
					n, e := src.Read(b)
					if e != nil {
						return
					}
					dst.SetWriteDeadline(time.Now().Add(10 * time.Second))
					if _, e = dst.Write(b[:n]); e != nil {
						return
					}
				}
			}
			go pump(in, out)
			go pump(out, in)
			<-done
			in.Close()
			out.Close()
			<-done
		}()
		return true
	})
	s.SetTransportProtocolHandler(udp.ProtocolNumber, uf.HandlePacket)
	return s, ep
}
func readFrame(r io.Reader) ([]byte, error) {
	var h [4]byte
	if _, e := io.ReadFull(r, h[:]); e != nil {
		return nil, e
	}
	n := binary.BigEndian.Uint32(h[:])
	if n < 14 || n > 65535 {
		return nil, errors.New("invalid Ethernet frame length")
	}
	b := make([]byte, n)
	_, e := io.ReadFull(r, b)
	return b, e
}
func arpReply(b []byte) []byte {
	if len(b) < 42 || binary.BigEndian.Uint16(b[12:14]) != 0x806 || binary.BigEndian.Uint16(b[14:16]) != 1 || binary.BigEndian.Uint16(b[16:18]) != 0x800 || b[18] != 6 || b[19] != 4 || binary.BigEndian.Uint16(b[20:22]) != 1 || !net.IP(b[38:42]).Equal(gatewayIP) {
		return nil
	}
	out := make([]byte, 42)
	copy(out[:6], b[6:12])
	copy(out[6:12], gatewayMAC)
	copy(out[12:22], b[12:22])
	out[21] = 2
	copy(out[22:28], gatewayMAC)
	copy(out[28:32], gatewayIP)
	copy(out[32:38], b[22:28])
	copy(out[38:42], b[28:32])
	return out
}
func serveEthernet(c net.Conn, ep *channel.Endpoint) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer c.Close()
	var mu sync.Mutex
	write := func(b []byte) error {
		mu.Lock()
		defer mu.Unlock()
		c.SetWriteDeadline(time.Now().Add(10 * time.Second))
		var h [4]byte
		binary.BigEndian.PutUint32(h[:], uint32(len(b)))
		_, e := io.Copy(c, io.MultiReader(bytesReader(h[:]), bytesReader(b)))
		return e
	}
	go func() {
		for {
			p := ep.ReadContext(ctx)
			if p == nil {
				return
			}
			v := p.ToView()
			b := make([]byte, 14+v.Size())
			copy(b, guestMAC)
			copy(b[6:], gatewayMAC)
			binary.BigEndian.PutUint16(b[12:14], 0x800)
			copy(b[14:], v.AsSlice())
			v.Release()
			p.DecRef()
			if write(b) != nil {
				c.Close()
				return
			}
		}
	}()
	for {
		b, e := readFrame(c)
		if e != nil {
			return e
		}
		switch binary.BigEndian.Uint16(b[12:14]) {
		case 0x806:
			if reply := arpReply(b); reply != nil {
				if e = write(reply); e != nil {
					return e
				}
			}
		case 0x800:
			p := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(b[14:])})
			ep.InjectInbound(ipv4.ProtocolNumber, p)
			p.DecRef()
		}
	}
}

type sliceReader struct{ b []byte }

func bytesReader(b []byte) *sliceReader { return &sliceReader{b} }
func (r *sliceReader) Read(p []byte) (int, error) {
	if len(r.b) == 0 {
		return 0, io.EOF
	}
	n := copy(p, r.b)
	r.b = r.b[n:]
	return n, nil
}
func listenGuest(s *stack.Stack, port uint16) (net.Listener, error) {
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		return nil, e
	}
	go func() {
		for {
			c, e := l.Accept()
			if e != nil {
				return
			}
			go func() {
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				g, e := gonet.DialContextTCP(ctx, s, tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4([4]byte{10, 77, 0, 2}), Port: port}, ipv4.ProtocolNumber)
				if e != nil {
					c.Close()
					return
				}
				relay(c, g)
			}()
		}
	}()
	return l, nil
}
func main() {
	iface := flag.String("interface", "", "physical adapter name")
	source := flag.String("source", "", "IPv4 on adapter")
	probe := flag.String("probe", "", "optional direct TCP IPv4:port connectivity probe; no VPN")
	flag.Parse()
	u, e := newUplink(*iface, *source)
	if e != nil {
		log.Fatal(e)
	}
	if *probe != "" {
		c, e := u.dial(context.Background(), "tcp4", *probe)
		if e != nil {
			log.Fatal(e)
		}
		fmt.Println("bound TCP connected:", c.LocalAddr(), "->", c.RemoteAddr())
		c.Close()
		return
	}
	s, ep := newStack(u)
	defer s.Close()
	defer ep.Close()
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		log.Fatal(e)
	}
	defer l.Close()
	forwards := map[string]string{}
	for name, port := range map[string]uint16{"ssh": 22, "vnc": 5901} {
		f, e := listenGuest(s, port)
		if e != nil {
			log.Fatal(e)
		}
		defer f.Close()
		forwards[name] = f.Addr().String()
	}
	forwards["ethernet"] = l.Addr().String()
	json.NewEncoder(os.Stdout).Encode(forwards)
	// The first connection is the launcher-owned QEMU. Limit the accept window.
	l.(*net.TCPListener).SetDeadline(time.Now().Add(30 * time.Second))
	c, e := l.Accept()
	if e != nil {
		log.Fatal(e)
	}
	l.Close()
	go func() {
		for {
			time.Sleep(time.Second)
			if e := u.check(); e != nil {
				log.Print(e)
				c.Close()
				return
			}
		}
	}()
	if e = serveEthernet(c, ep); e != nil {
		log.Print(e)
	}
}
