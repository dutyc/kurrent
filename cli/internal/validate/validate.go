// 命令参数语义校验（严格解析之后的第二步）：URL / 身份 / IP / 吊销原因 / 序列号。
package validate

import (
	"fmt"
	"net"
	"net/url"
	"regexp"
	"strings"
)

var (
	hexSerialRe = regexp.MustCompile(`^[0-9a-fA-F]{1,64}$`)
	tokenIDRe   = regexp.MustCompile(`^[0-9a-f]{6}$`)
	// 与控制面 pki.REVOCATION_REASONS 保持一致（RFC 5280 子集）。
	revocationReasons = map[string]bool{
		"unspecified":            true,
		"key_compromise":         true,
		"superseded":             true,
		"cessation_of_operation": true,
	}
)

// ValidateTokenID token 台账 key = token 明文点号前的前缀（6 位小写 hex）。
// 完整 token 含 secret，绝不能被拼进 URL 路径（会进服务端访问日志）。
func ValidateTokenID(raw string) (string, error) {
	id := strings.TrimSpace(raw)
	if strings.Contains(id, ".") {
		return "", fmt.Errorf("token_id is the prefix before the dot; do not paste the full token: %q", raw)
	}
	if !tokenIDRe.MatchString(id) {
		return "", fmt.Errorf("token_id must be 6 lowercase hex characters (see kurrent token list): %q", raw)
	}
	return id, nil
}

func ValidateAgentID(raw string) (string, error) {
	id := strings.TrimSpace(raw)
	if !ValidAgentID(id) {
		return "", fmt.Errorf("agent id must be 12 lowercase hex characters (machine identity; put descriptive names in --remark): %q", raw)
	}
	return id, nil
}

func ValidateHTTPURL(label, raw string) (string, error) {
	s := strings.TrimSpace(raw)
	u, err := url.Parse(s)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return "", fmt.Errorf("%s must be an http(s) URL: %q", label, raw)
	}
	if u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("%s must not contain credentials, a query string, or a fragment: %q", label, raw)
	}
	return strings.TrimRight(s, "/"), nil
}

// NormalizeCPURL 校验控制面入口并去掉误贴的 /api/cp 后缀：agent/nvmet-host 自己
// 在该地址后追加 /api/cp/enroll；粘贴 API 根会把路径拼成 /api/cp/api/cp/enroll。
// 其他路径前缀（反向代理）保留。
func NormalizeCPURL(label, raw string) (string, error) {
	s, err := ValidateHTTPURL(label, raw)
	if err != nil {
		return "", err
	}
	if strings.HasSuffix(s, "/api/cp") {
		s = strings.TrimRight(strings.TrimSuffix(s, "/api/cp"), "/")
	}
	return s, nil
}

func ValidateIP(label, raw string) (string, error) {
	s := strings.TrimSpace(raw)
	if s == "" {
		return "", nil
	}
	if net.ParseIP(s) == nil {
		return "", fmt.Errorf("%s must be an IP address: %q", label, raw)
	}
	return s, nil
}

func ValidateRevocationReason(raw string) (string, error) {
	reason := strings.TrimSpace(raw)
	if !revocationReasons[reason] {
		return "", fmt.Errorf("--reason accepts only unspecified|key_compromise|superseded|cessation_of_operation (got %q)", raw)
	}
	return reason, nil
}

func ValidateSerial(raw string) (string, error) {
	serial := strings.ToLower(strings.TrimSpace(raw))
	if serial == "" {
		return "", nil
	}
	// 与控制面一致：接受可选的 0x 前缀（列表输出不带前缀）
	serial = strings.TrimPrefix(serial, "0x")
	if !hexSerialRe.MatchString(serial) {
		return "", fmt.Errorf("--serial must be 1..64 hex digits (optional 0x prefix): %q", raw)
	}
	return serial, nil
}

// ValidAgentID 节点身份（agent_id）只接受 12 位小写 hex：证书 CN、控制面登记键与
// 状态文件名共用这一形态，任何宽松格式都会在证书/台账/文件系统之间产生歧义。
func ValidAgentID(id string) bool {
	if len(id) != 12 {
		return false
	}
	for _, c := range id {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}
