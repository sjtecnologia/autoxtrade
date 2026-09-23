'use client'

import { useEffect, useRef, useState } from 'react'
import { WS_URL } from '@/lib/constants'

export interface WsMessage {
  type: string
  payload: unknown
}

export function useWebSocket() {
  const [messages, setMessages] = useState<WsMessage[]>([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => setConnected(true)
    ws.onclose = () => setConnected(false)
    ws.onerror = () => setConnected(false)
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as WsMessage
        setMessages((prev) => [msg, ...prev].slice(0, 50))
      } catch {
        // mensagem não-JSON — ignora
      }
    }

    return () => {
      ws.close()
    }
  }, [])

  return { messages, connected }
}
