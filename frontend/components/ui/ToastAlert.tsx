'use client'

import clsx from 'clsx'
import { useEffect } from 'react'

interface ToastAlertProps {
  variant: 'success' | 'warning' | 'danger' | 'info'
  message: string
  onDismiss?: () => void
  autoDismissMs?: number
  fixed?: boolean
}

const variants = {
  success: 'border-emerald-500/40 bg-emerald-900/30 text-emerald-300',
  warning: 'border-yellow-500/40 bg-yellow-900/30 text-yellow-300',
  danger: 'border-red-500/40 bg-red-900/30 text-red-300',
  info: 'border-blue-500/40 bg-blue-900/30 text-blue-300',
}

export function ToastAlert({
  variant,
  message,
  onDismiss,
  autoDismissMs,
  fixed,
}: ToastAlertProps) {
  useEffect(() => {
    if (autoDismissMs && onDismiss) {
      const t = setTimeout(onDismiss, autoDismissMs)
      return () => clearTimeout(t)
    }
  }, [autoDismissMs, onDismiss])

  return (
    <div
      role="alert"
      className={clsx(
        'flex items-start gap-3 rounded-lg border px-4 py-3 text-sm',
        variants[variant],
        fixed && 'fixed bottom-4 right-4 z-50 shadow-xl max-w-sm',
      )}
    >
      <span className="flex-1">{message}</span>
      {onDismiss && (
        <button
          onClick={onDismiss}
          aria-label="Fechar"
          className="ml-2 text-current opacity-60 hover:opacity-100"
        >
          ✕
        </button>
      )}
    </div>
  )
}
