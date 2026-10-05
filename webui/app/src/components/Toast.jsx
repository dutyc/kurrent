import { useEffect, useRef } from 'react'
import './Toast.css'

// 结果通知：浮在右下角，不进页面流（页面流里的那行文字会被误当成内容的一部分）。
// 成功/部分成功自动消失（无人需要处理）；失败常驻，须手动关闭——错误不能被时间吞掉。
export default function Toast({ tone = 'ok', message, items = [], onClose, duration = 6000 }) {
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    if (tone === 'error' || !duration) return
    const timer = setTimeout(() => closeRef.current(), duration)
    return () => clearTimeout(timer)
  }, [tone, duration])

  return (
    <div className={`toast toast-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <div className="toast-head">
        <span className="toast-msg">{message}</span>
        <button type="button" className="toast-close" onClick={onClose} aria-label="close">
          ×
        </button>
      </div>
      {items.length > 0 && (
        <ul className="toast-items">
          {items.map((item) => (
            <li key={item.label}>
              <b>{item.label}</b>
              {item.detail ? ` ${item.detail}` : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function ToastStack({ children }) {
  return <div className="toast-stack">{children}</div>
}
