// 表格输出的共享取值与占位符。
package main

import (
	"encoding/json"
	"strconv"
)

// dash 空值显示占位符（表格对齐）。
func dash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

func strOf(v any) string {
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
