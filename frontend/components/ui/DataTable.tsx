'use client'

import clsx from 'clsx'
import React from 'react'

export interface Column<T> {
  key: keyof T | string
  header: string
  render?: (row: T) => React.ReactNode
  className?: string
}

interface DataTableProps<T> {
  columns: Column<T>[]
  data: T[]
  isLoading?: boolean
  emptyMessage?: string
  keyField?: string
}

export function DataTable<T>({
  columns,
  data,
  isLoading,
  emptyMessage = 'Nenhum dado encontrado.',
  keyField,
}: DataTableProps<T>) {
  if (isLoading) {
    return (
      <div className="space-y-2">
        {[...Array(5)].map((_, i) => (
          <div key={i} className="h-10 rounded-md bg-slate-800 animate-pulse" />
        ))}
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-700">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-700 bg-slate-800/50">
            {columns.map((col) => (
              <th
                key={String(col.key)}
                className={clsx(
                  'px-3 py-2 text-left text-xs font-medium uppercase tracking-wider text-slate-400',
                  col.className,
                )}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-3 py-8 text-center text-slate-500">
                {emptyMessage}
              </td>
            </tr>
          ) : (
            data.map((row, idx) => (
              <tr
                key={keyField ? String((row as Record<string, unknown>)[keyField as string]) : idx}
                className="border-b border-slate-800 hover:bg-slate-800/40 transition-colors"
              >
                {columns.map((col) => (
                  <td key={String(col.key)} className={clsx('px-3 py-2 text-slate-300', col.className)}>
                    {col.render ? col.render(row) : String((row as Record<string, unknown>)[col.key as string] ?? '—')}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
