package main

import (
	"flag"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/hashicorp/yamux"
)

func run() error {
	socket := flag.String("socket", "", "Owned Unix socket")
	engine := flag.String("engine", "", "Embedded engine path")
	flag.Parse()
	if !strings.HasPrefix(filepath.Clean(*socket), "/run/primigenius/") || !filepath.IsAbs(*engine) { return fmt.Errorf("invalid runtime paths") }
	if err := os.MkdirAll(filepath.Dir(*socket), 0700); err != nil { return err }
	lock, err := os.OpenFile(*socket+".launch-lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil { return err }
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil { return err }
	probe, err := net.DialTimeout("unix", *socket, time.Second)
	if err == nil { probe.Close() } else {
		logFile, err := os.OpenFile(*socket+".log", os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0600)
		if err != nil { return err }
		cmd := exec.Command(*engine, "--socket", *socket)
		cmd.Stdin = nil; cmd.Stdout = logFile; cmd.Stderr = logFile
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
		if err := cmd.Start(); err != nil { logFile.Close(); return err }
		go func() { _ = cmd.Wait() }()
		_ = logFile.Close()
		deadline := time.Now().Add(30 * time.Second)
		for {
			probe, err = net.DialTimeout("unix", *socket, time.Second)
			if err == nil { probe.Close(); break }
			if time.Now().After(deadline) { return fmt.Errorf("embedded engine startup timed out; inspect %s.log", *socket) }
			time.Sleep(100 * time.Millisecond)
		}
	}
	_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	session, err := yamux.Server(stdio{os.Stdin, os.Stdout, func() error { return os.Stdin.Close() }}, muxConfig())
	if err != nil { return err }
	defer session.Close()
	for {
		stream, err := session.Accept()
		if err != nil { return err }
		go func() {
			conn, err := net.DialTimeout("unix", *socket, 3*time.Second)
			if err != nil { stream.Close(); return }
			relay(stream, conn)
		}()
	}
}
