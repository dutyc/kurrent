import { useState } from 'react'
import { useI18n } from '../i18n'
import Button from './Button'
import './ConfirmAction.css'

export default function ConfirmAction({
  trigger,
  message,
  onConfirm,
  extraFields,
  details,
  confirmLabel,
  requireExtra,
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [extra, setExtra] = useState({})
  const gated = (requireExtra || []).some((name) => !extra[name])

  // 按钮 disabled 时不弹窗（trigger 内 Button 可能带 disabled）
  const handleOpen = (e) => {
    const btn = e.target.closest('button')
    if (btn && btn.disabled) return
    setOpen(true)
  }

  const handleClose = () => {
    setOpen(false)
    setExtra({})
  }

  const handleConfirm = () => {
    onConfirm(extra)
    setOpen(false)
    setExtra({})
  }

  return (
    <div className="confirm-wrap">
      <span onClick={handleOpen} className="confirm-trigger">
        {trigger}
      </span>
      {open && (
        <div className="confirm-overlay" onClick={handleClose}>
          <div className="confirm-box" onClick={(e) => e.stopPropagation()}>
            <p className="confirm-msg">{message}</p>
            {details && <div className="confirm-details">{details}</div>}
            {extraFields && (
              <div className="confirm-extra">
                {extraFields.map((f) => (
                  <label key={f.name} className="confirm-check">
                    <input
                      type="checkbox"
                      checked={!!extra[f.name]}
                      onChange={(e) =>
                        setExtra((prev) => ({ ...prev, [f.name]: e.target.checked }))
                      }
                    />
                    <span>{f.label}</span>
                  </label>
                ))}
              </div>
            )}
            <div className="confirm-actions">
              <Button variant="danger" onClick={handleConfirm} disabled={gated}>
                {confirmLabel || t('workerDetail.confirm')}
              </Button>
              <Button variant="ghost" onClick={handleClose}>
                {t('workers.cancel')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
