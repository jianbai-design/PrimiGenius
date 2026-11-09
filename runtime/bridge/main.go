package main

import (
	"io"
	"log"
	"net"
	"os"
	"sync"
	"time"

	"github.com/hashicorp/yamux"
)

// stdout belongs exclusively to the framed transport. Diagnostics use stderr.
type stdio struct { io.Reader; io.Writer; close func() error }
func (s stdio) Close() error { return s.close() }

func muxConfig() *yamux.Config {
	c := yamux.DefaultConfig()
	c.LogOutput = os.Stderr
	c.KeepAliveInterval = 15 * time.Second
	c.ConnectionWriteTimeout = 15 * time.Second
	c.StreamCloseTimeout = 5 * time.Second
	c.MaxStreamWindowSize = 256 * 1024
	return c
}

// Drain the opposite direction on EOF; never truncate the tail of a response.
func relay(a net.Conn, b net.Conn) {
	defer a.Close()
	defer b.Close()
	var wg sync.WaitGroup
	wg.Add(2)
	copyOne := func(dst, src net.Conn) {
		defer wg.Done()
		_, err := io.Copy(dst, src)
		if cw, ok := dst.(interface{ CloseWrite() error }); ok {
			_ = cw.CloseWrite()
		} else if stream, ok := dst.(*yamux.Stream); ok {
			_ = stream.Close() // yamux FIN closes writes, leaving reads available.
		}
		if err != nil { _ = src.Close(); _ = dst.Close() }
	}
	go copyOne(a, b)
	go copyOne(b, a)
	wg.Wait()
}

func main() {
	log.SetOutput(os.Stderr)
	if err := run(); err != nil { log.Print(err); os.Exit(1) }
}
