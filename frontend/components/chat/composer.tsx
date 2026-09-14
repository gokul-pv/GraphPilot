'use client'

import { useRef, useState } from 'react'
import { ArrowUp, Square } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/**
 * The query box.
 *
 * Enter submits, Shift+Enter breaks the line — the convention for this shape
 * of input, and queries here are often a paragraph rather than a sentence.
 */
export function Composer({
  onSubmit,
  onCancel,
  busy,
  disabled,
}: {
  onSubmit: (query: string) => void
  onCancel: () => void
  busy: boolean
  disabled?: boolean
}) {
  const [value, setValue] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)

  const submit = () => {
    const query = value.trim()
    if (!query || busy || disabled) return
    onSubmit(query)
    setValue('')
    // Reset the auto-grow so the box does not stay tall after sending.
    if (ref.current) ref.current.style.height = 'auto'
  }

  return (
    <div className="shrink-0 border-t bg-background p-3">
      <div
        className={cn(
          'flex items-end gap-2 rounded-xl border bg-card p-2 transition-shadow',
          'focus-within:ring-2 focus-within:ring-ring/40',
        )}
      >
        <textarea
          ref={ref}
          value={value}
          rows={1}
          disabled={disabled}
          placeholder={
            disabled ? 'Connecting to the API…' : 'Ask GraphPilot to do something…'
          }
          onChange={(e) => {
            setValue(e.target.value)
            e.target.style.height = 'auto'
            e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              submit()
            }
          }}
          className="max-h-50 min-h-8 flex-1 resize-none bg-transparent px-1.5 py-1 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-50"
        />

        {busy ? (
          <Button
            size="icon"
            variant="secondary"
            onClick={onCancel}
            className="size-8 shrink-0"
            title="Cancel this run"
          >
            <Square className="size-3.5 fill-current" />
          </Button>
        ) : (
          <Button
            size="icon"
            onClick={submit}
            disabled={!value.trim() || disabled}
            className="size-8 shrink-0"
            title="Send"
          >
            <ArrowUp className="size-4" />
          </Button>
        )}
      </div>

      <p className="mt-1.5 px-1 text-[10px] text-muted-foreground">
        Each query starts its own DAG. Runs execute one at a time — a second
        one queues.
      </p>
    </div>
  )
}
