import { useMutation } from '@tanstack/react-query'
import { Copy, ExternalLink, EyeOff } from 'lucide-react'
import { useEffect, useState } from 'react'
import { apiPost } from '../api/client'
import type { ApplicationRow } from '../api/types'
import { UNAVAILABLE_HINT } from '../lib/vacancyView'
import { Button } from './ui/Button'
import { Textarea } from './ui/Field'
import { ErrorNotice } from './ui/States'

/** How the response ended: sent, not sent (the vacancy goes back), or hh.ru does not show the vacancy any more (the
 *  response is cancelled and the vacancy hidden). */
export type AssistResult = 'sent' | 'not_sent' | 'unavailable'

/** Mode B: letter copied to the clipboard, hh.ru response page opened, the user confirms the result. */
export default function AssistPanel({
  application,
  onDone,
  onNext,
  remaining,
}: {
  application: ApplicationRow
  onDone: (result: AssistResult) => void
  onNext?: () => void
  remaining?: number
}) {
  const [copied, setCopied] = useState<boolean | null>(null)
  const confirm = useMutation({
    mutationFn: (sent: boolean) => apiPost<ApplicationRow>(`/api/applications/${application.id}/confirm`, { sent }),
    onSuccess: (_r, sent) => onDone(sent ? 'sent' : 'not_sent'),
  })
  // «Вам недоступна эта вакансия» on hh.ru: one click instead of «Не отправил» and hiding it
  const unavailable = useMutation({
    mutationFn: () => apiPost(`/api/vacancies/${application.vacancy_id}/unavailable`, {}),
    onSuccess: () => onDone('unavailable'),
  })
  const busy = confirm.isPending || unavailable.isPending

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
        <Button variant="primary" onClick={() => confirm.mutate(true)} disabled={busy}>
          Да, отправлено
        </Button>
        <Button onClick={() => confirm.mutate(false)} disabled={busy}>
          Не отправил
        </Button>
        <Button onClick={() => unavailable.mutate()} disabled={busy} title={UNAVAILABLE_HINT}>
          <EyeOff aria-hidden />
          Вакансия недоступна
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
      {(confirm.error || unavailable.error) && <ErrorNotice className="mt-3" error={confirm.error ?? unavailable.error} title="Ответ не записан" />}
    </section>
  )
}
