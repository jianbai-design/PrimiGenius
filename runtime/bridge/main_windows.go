package main

import (
	"flag"
	"fmt"
	"os"
	"os/exec"
 "net"
	"strings"
	"syscall"
	"unsafe"

	"github.com/Microsoft/go-winio"
	"github.com/hashicorp/yamux"
	"golang.org/x/sys/windows"
)

func run() error {
	pipe := flag.String("pipe", "", "Owned Windows pipe")
	distro := flag.String("distro", "", "Owned WSL distribution")
	helper := flag.String("helper", "", "Linux bridge path")
	engine := flag.String("engine", "", "Embedded Libpod engine path")
	socket := flag.String("socket", "", "Owned engine Unix socket")
	flag.Parse()
	if !strings.HasPrefix(*pipe, `\\.\pipe\primigenius-`) || *distro == "" || !strings.HasPrefix(*helper, "/") || !strings.HasPrefix(*engine, "/") || !strings.HasPrefix(*socket, "/run/primigenius/") {
		return fmt.Errorf("invalid runtime bridge arguments")
	}
	token, err := windows.OpenCurrentProcessToken()
	if err != nil { return err }
	defer token.Close()
	user, err := token.GetTokenUser()
	if err != nil { return err }
	listener, err := winio.ListenPipe(*pipe, &winio.PipeConfig{
		SecurityDescriptor: "D:P(A;;GA;;;" + user.User.Sid.String() + ")(A;;GA;;;SY)",
		MessageMode: true, InputBufferSize: 65536, OutputBufferSize: 65536,
	})
	if err != nil { return err }
	defer listener.Close()
    control, err := winio.ListenPipe(*pipe+"-control", &winio.PipeConfig{
        SecurityDescriptor: "D:P(A;;GA;;;" + user.User.Sid.String() + ")(A;;GA;;;SY)",
        MessageMode: true, InputBufferSize: 65536, OutputBufferSize: 65536,
    })
    if err != nil { return err }
    defer control.Close()

	cmd := exec.Command("wsl.exe", "-d", *distro, "-u", "root", "--exec", *helper, "--socket", *socket, "--engine", *engine)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: windows.CREATE_NO_WINDOW}
	in, err := cmd.StdinPipe(); if err != nil { return err }
	out, err := cmd.StdoutPipe(); if err != nil { return err }
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil { return err }
	defer func() { _ = in.Close(); _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil { return err }
	defer windows.CloseHandle(job)
	limits := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	limits.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation, uintptr(unsafe.Pointer(&limits)), uint32(unsafe.Sizeof(limits))); err != nil { return err }
	processHandle, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err != nil { return err }
	err = windows.AssignProcessToJobObject(job, processHandle)
	windows.CloseHandle(processHandle)
	if err != nil { return err }
	session, err := yamux.Client(stdio{out, in, in.Close}, muxConfig())
	if err != nil { return err }
	defer session.Close()

    go func() { <-session.CloseChan(); _ = listener.Close(); _ = control.Close() }()
    errors := make(chan error, 2)
    serve := func(listener net.Listener, limit int) {
        slots := make(chan struct{}, limit)
        for {
            conn, err := listener.Accept()
            if err != nil { errors <- err; return }
            select { case slots <- struct{}{}: case <-session.CloseChan(): conn.Close(); errors <- session.Close(); return }
            go func() {
                defer func() { <-slots }()
                stream, err := session.Open()
                if err != nil { conn.Close(); return }
                relay(conn, stream)
            }()
        }
    }
    // Reserve streams for status and cancellation even during bulk log/build I/O.
    go serve(listener, 96)
    go serve(control, 16)
    return <-errors
}
