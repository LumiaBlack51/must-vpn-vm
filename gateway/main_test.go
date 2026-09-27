package main

import (
 "bytes"
 "context"
 "encoding/binary"
 "net"
 "testing"
)

func TestFrameLengthBound(t *testing.T){
 for _,n:=range []uint32{0,13,65536,0xffffffff}{var b [4]byte;binary.BigEndian.PutUint32(b[:],n);if _,e:=readFrame(bytes.NewReader(b[:]));e==nil{t.Fatalf("accepted %d",n)}}
}
func TestFramePartialRead(t *testing.T){
 b:=make([]byte,18);binary.BigEndian.PutUint32(b[:4],14);copy(b[4:],[]byte("fixture-frame!"));out,e:=readFrame(bytes.NewReader(b));if e!=nil||len(out)!=14{t.Fatal(e)}
 if _,e=readFrame(bytes.NewReader(b[:17]));e==nil{t.Fatal("accepted truncated frame")}
}
func TestARP(t *testing.T){
 b:=make([]byte,42);copy(b[6:12],guestMAC);binary.BigEndian.PutUint16(b[12:14],0x806);binary.BigEndian.PutUint16(b[14:16],1);binary.BigEndian.PutUint16(b[16:18],0x800);b[18]=6;b[19]=4;b[21]=1;copy(b[22:28],guestMAC);copy(b[28:32],[]byte{10,77,0,2});copy(b[38:42],gatewayIP)
 r:=arpReply(b);if len(r)!=42||r[21]!=2||!bytes.Equal(r[:6],guestMAC)||!bytes.Equal(r[28:32],gatewayIP){t.Fatal("invalid ARP response")}
 b[41]=99;if arpReply(b)!=nil{t.Fatal("answered for a different gateway")}
}
func TestMissingUplinkNeverFallsBack(t *testing.T){
 u:=&uplink{name:"nonexistent-must-adapter",index:9999,ip:net.IPv4(192,0,2,10)}
 for _,network:=range []string{"tcp4","udp4"}{if c,e:=u.dial(context.Background(),network,"1.1.1.1:53");e==nil{c.Close();t.Fatal("fell back to default route")}}
}
