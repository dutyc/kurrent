// 编排清单静态断言：项目名固定、运行时状态挂载源只来自状态根环境变量、
// 旧仓库内相对状态路径不再出现。
package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

type composeDoc struct {
	Name     string `yaml:"name"`
	Services map[string]struct {
		Volumes []string `yaml:"volumes"`
	} `yaml:"services"`
}

func readCompose(t *testing.T, path string) composeDoc {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var doc composeDoc
	if err := yaml.Unmarshal(data, &doc); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	return doc
}

// splitVolume 按顶层冒号切分挂载三元组；${VAR:?...} 内的冒号不算分隔符。
func splitVolume(volume string) []string {
	var parts []string
	depth, start := 0, 0
	for i, r := range volume {
		switch r {
		case '{':
			depth++
		case '}':
			if depth > 0 {
				depth--
			}
		case ':':
			if depth == 0 {
				parts = append(parts, volume[start:i])
				start = i + 1
			}
		}
	}
	return append(parts, volume[start:])
}

func volumeSource(volume string) string {
	return splitVolume(volume)[0]
}

var legacyStateSources = []string{
	"./control_plane/state",
	"./control_plane/config",
	"./dnsmasq",
	"../state",
	"../bootstrap",
	"../agent/logs",
	"../../control_plane/state/certs",
}

func TestRootComposePinsProjectAndStateRoot(t *testing.T) {
	doc := readCompose(t, filepath.Join("..", "docker-compose.yml"))
	if doc.Name != defaultComposeProject {
		t.Fatalf("name = %q, want %q", doc.Name, defaultComposeProject)
	}
	var stateMounts int
	for service, svc := range doc.Services {
		for _, volume := range svc.Volumes {
			source := volumeSource(volume)
			for _, legacy := range legacyStateSources {
				if source == legacy || strings.HasPrefix(source, legacy+"/") {
					t.Errorf("%s still mounts legacy in-repository state: %s", service, volume)
				}
			}
			if strings.Contains(source, "KURRENT_STATE_DIR") || strings.Contains(source, "KURRENT_NODE_STATE_DIR") || strings.Contains(source, "KURRENT_CP_CA_FILE") {
				stateMounts++
			}
			if strings.Contains(volume, "/app/state") || strings.Contains(volume, "/app/bootstrap") ||
				strings.Contains(volume, "/app/config/agents.yml") || strings.Contains(volume, "/app/dnsmasq") {
				if !strings.Contains(source, "${KURRENT_STATE_DIR") {
					t.Errorf("%s state mount is not sourced from KURRENT_STATE_DIR: %s", service, volume)
				}
			}
		}
	}
	if stateMounts == 0 {
		t.Fatal("no state-root mounts found in the root compose file")
	}
}

func TestStorageComposePinsProjectAndNodeState(t *testing.T) {
	for _, file := range []string{
		filepath.Join("..", "storager", "nvmeof", "docker-compose.yml"),
		filepath.Join("..", "storager", "iscsi", "docker-compose.yml"),
	} {
		doc := readCompose(t, file)
		if doc.Name != defaultStorageProject {
			t.Errorf("%s: name = %q, want %q", file, doc.Name, defaultStorageProject)
		}
		destinations := map[string]string{}
		for service, svc := range doc.Services {
			for _, volume := range svc.Volumes {
				source := volumeSource(volume)
				parts := splitVolume(volume)
				if len(parts) >= 2 {
					destinations[parts[1]] = source
				}
				for _, legacy := range legacyStateSources {
					if source == legacy || strings.HasPrefix(source, legacy+"/") {
						t.Errorf("%s: %s still mounts legacy state: %s", service, service, volume)
					}
				}
			}
		}
		for _, dest := range []string{"/etc/kurrent/state", "/etc/kurrent/bootstrap", "/etc/kurrent/pki", "/etc/kurrent/cp-ca.crt", "/var/log/kurrent"} {
			source, ok := destinations[dest]
			if !ok {
				t.Errorf("%s: missing mount for %s", file, dest)
				continue
			}
			if dest == "/etc/kurrent/cp-ca.crt" {
				if !strings.Contains(source, "${KURRENT_CP_CA_FILE") {
					t.Errorf("%s: %s is not sourced from KURRENT_CP_CA_FILE: %s", file, dest, source)
				}
				continue
			}
			if !strings.Contains(source, "${KURRENT_NODE_STATE_DIR") {
				t.Errorf("%s: %s is not sourced from KURRENT_NODE_STATE_DIR: %s", file, dest, source)
			}
		}
	}
}
