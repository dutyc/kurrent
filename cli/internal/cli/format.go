// 表格输出的共享取值与占位符。
package cli

import (
	"encoding/json"
	"strconv"
)

// Dash 空值显示占位符（表格对齐）。
func Dash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

func StrOf(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	default:
		b, err := json.Marshal(t)
		if err != nil {
			return ""
		}
		return string(b)
	}
}
