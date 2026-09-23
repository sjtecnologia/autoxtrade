import type { Metadata } from 'next'
import Link from 'next/link'
import './globals.css'
import { Providers } from '@/lib/providers'

export const metadata: Metadata = {
  title: 'autoxtrade',
  description: 'Dashboard do robot de trading automatizado',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-BR" className="dark">
      <body className="min-h-screen bg-slate-900 text-white antialiased">
        <Providers>
          <nav className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-40">
            <div className="mx-auto w-full max-w-[1800px] px-3 sm:px-4 lg:px-5 py-3 flex items-center gap-6">
              <span className="text-sm font-bold text-blue-400 tracking-tight">autoxtrade</span>
              <Link
                href="/"
                className="text-sm text-slate-300 hover:text-white transition-colors"
              >
                Dashboard
              </Link>
              <Link
                href="/charts"
                className="text-sm text-slate-300 hover:text-white transition-colors"
              >
                Gráficos
              </Link>
              <Link
                href="/history"
                className="text-sm text-slate-300 hover:text-white transition-colors"
              >
                Histórico
              </Link>
              <Link
                href="/models"
                className="text-sm text-slate-300 hover:text-white transition-colors"
              >
                Modelos ML
              </Link>
              <Link
                href="/settings"
                className="text-sm text-slate-300 hover:text-white transition-colors"
              >
                Configurações
              </Link>
              <Link
                href="/approvals"
                className="text-sm text-slate-300 hover:text-white transition-colors"
              >
                Aprovações
              </Link>
            </div>
          </nav>
          <main className="mx-auto w-full max-w-[1800px] px-3 sm:px-4 lg:px-5 py-5">{children}</main>
        </Providers>
      </body>
    </html>
  )
}
