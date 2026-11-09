//go:build linux

// Built inside the pinned upstream source tree so its vendored dependencies,
// build tags and storage ABI are identical to the validated engine baseline.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	goruntime "runtime"
	"strings"
	"syscall"
	"time"

	"github.com/containers/podman/v5/libpod"
	api "github.com/containers/podman/v5/pkg/api/server"
	"github.com/containers/podman/v5/pkg/domain/entities"
	"github.com/containers/podman/v5/pkg/domain/infra"
	"github.com/containers/podman/v5/pkg/parallel"
	"github.com/containers/podman/v5/version"
	"github.com/sirupsen/logrus"
	"go.podman.io/storage/pkg/reexec"
)

var sourceCommit = "unknown"

func main() {
	if reexec.Init() { return }
	// Libpod's conmon callbacks invoke os.Executable(). Keep the upstream Linux
	// cleanup helper for these internal callbacks, never for the engine service.
	for i := 1; i+1 < len(os.Args); i++ {
		if (os.Args[i] == "container" && os.Args[i+1] == "cleanup") || (os.Args[i] == "healthcheck" && os.Args[i+1] == "run") {
			if err := syscall.Exec("/usr/bin/podman", append([]string{"podman"}, os.Args[1:]...), os.Environ()); err != nil { log.Fatal(err) }
		}
	}
	if err := run(); err != nil { log.Print(err); os.Exit(1) }
}

func run() error {
	socket := flag.String("socket", "", "Owned Unix socket")
	showVersion := flag.Bool("version", false, "Print embedded engine identity")
	flag.Parse()
	if *showVersion {
		return json.NewEncoder(os.Stdout).Encode(map[string]string{"engine":"primigenius-libpod", "version":version.Version.String(), "commit":sourceCommit})
	}
	path := filepath.Clean(*socket)
	if !strings.HasPrefix(path, "/run/primigenius/") { return fmt.Errorf("engine socket must be in /run/primigenius") }
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil { return err }
	lock, err := os.OpenFile(path+".engine-lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil { return err }
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil { return fmt.Errorf("engine already owns socket: %w", err) }
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSocket == 0 { return fmt.Errorf("refusing to remove non-socket %s", path) }
		if err := os.Remove(path); err != nil { return err }
	}
	logrus.SetOutput(os.Stderr)
	logrus.SetLevel(logrus.WarnLevel)
	_ = syscall.Umask(0077)
	if err := parallel.SetMaxThreads(uint(goruntime.NumCPU() * 3 + 1)); err != nil { return err }
	runtime, err := libpod.NewRuntime(context.Background())
	if err != nil { return err }
	defer runtime.Shutdown(false)
	runtime.SetRemoteURI("unix://"+path)
	listener, err := net.Listen("unix", path)
	if err != nil { return err }
	defer listener.Close()
	infra.StartWatcher(runtime)
	server, err := api.NewServerWithSettings(runtime, listener, entities.ServiceOptions{Timeout:0})
	if err != nil { return err }
	handler := server.Handler
	server.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-PrimiGenius-Engine", sourceCommit)
		if r.URL.Path == "/primigenius/runtime" && r.Method == http.MethodGet {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]string{"engine":"primigenius-libpod", "version":version.Version.String(), "commit":sourceCommit})
			return
		}
		if r.URL.Path == "/primigenius/shutdown" && r.Method == http.MethodPost {
			w.WriteHeader(http.StatusAccepted)
			go func() { time.Sleep(100*time.Millisecond); _ = server.Shutdown(true) }()
			return
		}
		handler.ServeHTTP(w, r)
	})
	return server.Serve()
}
