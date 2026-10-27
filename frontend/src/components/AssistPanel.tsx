import { useMutation } from '@tanstack/react-query'
import { Copy, ExternalLink } from 'lucide-react'
import { useEffect, useState } from 'react'
import { apiPost } from '../api/client'
import type { ApplicationRow } from '../api/types'
import { Button } from './ui/Button'
import { Textarea } from './ui/Field'
import { ErrorNotice } from './ui/States'

/** Mode B: letter copied to the clipboard, hh.ru response page opened, the user confirms the result. */
export default function AssistPanel({
  application,
  onDone,
  onNext,
  remaining,
}: {
  application: ApplicationRow
  onDone: (sent: boolean) => void
  onNext?: () => void
  remaining?: number
}) {
  const [copied, setCopied] = useState<boolean | null>(null)
  const confirm = useMutation({
    mutationFn: (sent: boolean) => apiPost<ApplicationRow>(`/api/applications/${application.id}/confirm`, { sent }),
    onSuccess: (_r, sent) => onDone(sent),
  })

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(application.letter_text ?? '')
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  useEffect(() => {
    void copy()
    if (application.apply_url) window.open(application.apply_url, '_blank', 'noopener')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [application.id])

  return (
    <section aria-label="Отклик на hh.ru" className="rounded-lg border border-accent/25 bg-accent-soft p-4 sm:p-5">
      <h2 className="text-[15px] font-semibold text-ink">
        Отклик на hh.ru: {application.vacancy?.name}
        {remaining !== undefined && remaining > 0 && <span className="ml-2 text-caption font-normal text-ink-3">ещё в очереди: {remaining}</span>}
      </h2>
      <ol className="mt-2 list-decimal space-y-1.5 pl-5 text-sm text-ink-2">
        <li>
          Страница отклика открыта в новой вкладке.{' '}
          {application.apply_url && (
            <a className="inline-flex items-center gap-1 font-medium text-accent underline-offset-2 hover:underline" href={application.apply_url} target="_blank" rel="noreferrer">
              Открыть ещё раз
              <ExternalLink aria-hidden className="size-3.5" />
            </a>
          )}
        </li>
        <li>
          Письмо {copied === true ? 'скопировано в буфер обмена' : copied === false ? 'не удалось скопировать автоматически' : 'копируется…'}: вставьте его в поле сопроводительного письма
          (Ctrl+V).
        </li>
        <li>Нажмите «Откликнуться» на hh.ru и подтвердите здесь.</li>
      </ol>
      {application.letter_text && <Textarea className="mt-3 min-h-28 bg-surface" aria-label="Текст письма" readOnly value={application.letter_text} onFocus={(e) => e.currentTarget.select()} />}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={() => confirm.mutate(true)} disabled={confirm.isPending}>
          Да, отправлено
        </Button>
        <Button onClick={() => confirm.mutate(false)} disabled={confirm.isPending}>
          Не отправил
        </Button>
        <Button variant="ghost" onClick={() => void copy()}>
          <Copy aria-hidden />
          Скопировать письмо снова
        </Button>
        {onNext && (
          <Button variant="ghost" onClick={onNext}>
            Пропустить и открыть следующую
          </Button>
        )}
      </div>
      {confirm.error && <ErrorNotice className="mt-3" error={confirm.error} title="Ответ не записан" />}
    </section>
  )
}
