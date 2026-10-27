import type { Capability, ProbeStep } from '../api/types'
import { Badge, type BadgeTone } from './ui/Badge'
import { Table, Td, Th } from './ui/Table'

const CAP_TONE: Record<Capability, BadgeTone> = {
  available: 'good',
  unavailable: 'bad',
  unknown: 'warn',
  skipped: 'neutral',
}

const CAP_LABEL: Record<Capability, string> = {
  available: 'доступен',
  unavailable: 'недоступен',
  unknown: 'неясно',
  skipped: 'пропущен',
}

export function CapabilityBadge({ value }: { value: Capability | string | undefined }) {
  const cap = (value ?? 'unknown') as Capability
  return <Badge tone={CAP_TONE[cap] ?? 'warn'}>{CAP_LABEL[cap] ?? cap}</Badge>
}

/** The steps of the hh.ru API probe: what was checked, the method, the HTTP code and the verdict. */
export default function ProbeTable({ steps }: { steps: ProbeStep[] }) {
  return (
    <Table label="Результат зонда" minWidth={720}>
      <thead>
        <tr>
          <Th>Проверка</Th>
          <Th>Метод</Th>
          <Th align="right">HTTP</Th>
          <Th>Итог</Th>
          <Th>Примечание</Th>
        </tr>
      </thead>
      <tbody>
        {steps.map((s) => (
          <tr key={s.key}>
            <Td className="font-medium">{s.title}</Td>
            <Td className="whitespace-nowrap font-mono text-xs text-ink-2">
              {s.method} {s.path}
            </Td>
            <Td align="right">{s.status ?? '—'}</Td>
            <Td>
              <CapabilityBadge value={s.capability} />
            </Td>
            <Td className="text-caption text-ink-2">{s.note}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  )
}
