//go:build windows

package jobs

import (
	"os"
	"syscall"
	"unsafe"
)

var sysProcAttr = &syscall.SysProcAttr{}

func killGroup(pid int) {
	p, err := os.FindProcess(pid)
	if err == nil {
		_ = p.Kill()
	}
}

var (
	modKernel32          = syscall.NewLazyDLL("kernel32.dll")
	procOpenProcess      = modKernel32.NewProc("OpenProcess")
	procGetExitCodeProc  = modKernel32.NewProc("GetExitCodeProcess")
	procCloseHandle      = modKernel32.NewProc("CloseHandle")
)

const (
	processQueryLimitedInformation = 0x1000
	stillActive                    = 259
)

// pidAlive returns true only when the process with the given PID is actually
// running. On Windows, os.Process.Signal(0) always errors (only os.Interrupt
// is supported), so we use GetExitCodeProcess: it returns STILL_ACTIVE (259)
// for a live process and the real exit code once it has exited.
func pidAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	handle, _, _ := procOpenProcess.Call(
		uintptr(processQueryLimitedInformation),
		0,
		uintptr(pid),
	)
	if handle == 0 {
		return false
	}
	defer procCloseHandle.Call(handle)
	var exitCode uint32
	ret, _, _ := procGetExitCodeProc.Call(handle, uintptr(unsafe.Pointer(&exitCode)))
	return ret != 0 && exitCode == stillActive
}
