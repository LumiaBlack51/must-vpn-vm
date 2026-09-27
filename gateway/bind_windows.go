package main

import (
	"encoding/binary"
	"syscall"
)

// IP_UNICAST_IF expects the interface index in network byte order.
func bindInterface(fd uintptr, name string, index int) error {
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], uint32(index))
	return syscall.SetsockoptInt(syscall.Handle(fd), syscall.IPPROTO_IP, 31, int(binary.LittleEndian.Uint32(b[:])))
}
