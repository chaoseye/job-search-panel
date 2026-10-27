import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiPost } from '../api/client'
import type { ApplyStatus } from '../api/types'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { ErrorNotice } from './ui/States'

/** First-time consent before any application is sent on the user's behalf. */
export default function ConsentModal({ onAccepted, onClose }: { onAccepted: () => void; onClose: () => void }) {
  const qc = useQueryClient()
  const accept = useMutation({
    mutationFn: () => apiPost<ApplyStatus>('/api/applications/consent'),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['apply-status'] })
      onAccepted()
    },
  })
  return (
    <Dialog onClose={onClose} labelledBy="consent-title">
      {(close) => (
        <>
          <h2 id="consent-title" className="text-lg font-semibold text-ink">
            Согласие на отправку откликов
          </h2>
          <div className="mt-3 space-y-2 text-sm text-ink-2">
            <p>Панель действует от вашего имени в вашем аккаунте hh.ru и отправляет отклики на выбранные вами вакансии с вашим резюме и сопроводительным письмом.</p>
            <p>Отправка откликов, их содержание и соблюдение правил hh.ru — ваша ответственность. Панель соблюдает лимиты и не обходит ограничения сайта.</p>
            <p>
              Используя панель, вы подтверждаете согласие с{' '}
              <a className="font-medium text-accent underline underline-offset-2" href="https://hh.ru/article/1175" target="_blank" rel="noreferrer">
                правилами hh.ru
              </a>{' '}
              и условиями оказания услуг по содействию в трудоустройстве.
            </p>
          </div>
          {accept.error && <ErrorNotice className="mt-3" error={accept.error} title="Согласие не сохранено" />}
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              Отмена
            </Button>
            <Button variant="primary" onClick={() => accept.mutate()} disabled={accept.isPending}>
              Подтверждаю
            </Button>
          </div>
        </>
      )}
    </Dialog>
  )
}
