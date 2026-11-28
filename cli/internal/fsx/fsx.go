// 文件系统与平台路径工具：可执行文件位置、用户主目录、原子写、目录权限与摘要。
package fsx

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// ExeDir 返回可执行文件所在目录（软链已解析）；变量化以便测试注入。
var ExeDir = func() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	return filepath.Dir(exe)
}

// UserHomeDir 返回用户主目录；变量化以便测试注入。
var UserHomeDir = func() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return home
}

// UserCredentialsDir 用户级凭据目录（~/.kurrent）；主目录不可知时返回 ""。
func UserCredentialsDir() string {
	if home := UserHomeDir(); home != "" {
		return filepath.Join(home, ".kurrent")
	}
	return ""
}

// UserCredentialsPath 用户级快照路径（~/.kurrent/admin.conf）；主目录不可知时返回 ""。
func UserCredentialsPath() string {
	if dir := UserCredentialsDir(); dir != "" {
		return filepath.Join(dir, "admin.conf")
	}
	return ""
}

// FileExists 判断路径存在且为普通文件。
func FileExists(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && !fi.IsDir()
}

// WriteFileAtomic 同目录临时文件 + rename（防半写；文件含私钥/密钥时权限恒为 perm）。
// CreateTemp 生成唯一临时名：并发 install/join 不会互相覆盖，也不会跟随预置的 .tmp 路径。
func WriteFileAtomic(path string, data []byte, perm os.FileMode) error {
	f, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer func() {
		if tmp != "" {
			os.Remove(tmp)
		}
	}()
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Chmod(perm); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	tmp = ""
	return nil
}

// EnsurePrivateDir 创建/收敛私有目录（0700；状态与凭据不含 group/other 权限）。
func EnsurePrivateDir(path string) error {
	if err := os.MkdirAll(path, 0o700); err != nil {
		return fmt.Errorf("create %s: %w", path, err)
	}
	if err := os.Chmod(path, 0o700); err != nil {
		return fmt.Errorf("chmod %s: %w", path, err)
	}
	return nil
}

// HashFile 计算文件 SHA-256（十六进制）。
func HashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// IsDir 判断路径存在且为目录。
func IsDir(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && fi.IsDir()
}
