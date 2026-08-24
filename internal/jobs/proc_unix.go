//go:build !windows

package jobs

import (
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

var sysProcAttr = &syscall.SysProcAttr{Setsid: true}

func killGroup(pid int) {
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil {
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
}

func pidAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	if err := proc.Signal(syscall.Signal(0)); err != nil {
		return false
	}
	out, err := exec.Command("ps", "-o", "stat=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return false
	}
	state := strings.TrimSpace(string(out))
	if state == "" || strings.HasPrefix(state, "Z") {
		return false
	}
	return true
}

