package main

import "syscall"

func bindInterface(fd uintptr, name string, index int) error {
	return syscall.SetsockoptString(int(fd), syscall.SOL_SOCKET, syscall.SO_BINDTODEVICE, name)
}
