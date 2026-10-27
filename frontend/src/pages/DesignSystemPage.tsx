import { Clock, Link2, MoreHorizontal, SlidersHorizontal } from 'lucide-react'
import { useState } from 'react'
import { ApiError } from '../api/client'
import { Badge, Count } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Chip, ToggleChip } from '../components/ui/Chip'
import { Dialog } from '../components/ui/Dialog'
import { Disclosure } from '../components/ui/Disclosure'
import { Dl } from '../components/ui/Dl'
import { Checkbox, Field, Input, Select, Textarea } from '../components/ui/Field'
import { Menu } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Pagination } from '../components/ui/Pagination'
import { Panel } from '../components/ui/Panel'
import { PipelineTrack } from '../components/ui/PipelineTrack'
import { Popover } from '../components/ui/Popover'
import { ScoreChip, ScoreMeter } from '../components/ui/Score'
import { SearchInput } from '../components/ui/SearchInput'
import { Segmented } from '../components/ui/Segmented'
import { Sheet, SheetCloseButton } from '../components/ui/Sheet'
import { Stat } from '../components/ui/Stat'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { SortTh, Table, Td, Th } from '../components/ui/Table'
import { Tabs } from '../components/ui/Tabs'

const COLORS: [string, string, string][] = [
  ['canvas', '#f6f7fa', 'фон страницы и боковой панели'],
  ['surface', '#fefeff', 'карточки, таблицы, панели'],
  ['sunken', '#f1f3f7', 'наведение, заливка бейджей и чипов'],
  ['selected', '#e7ebf1', 'текущий раздел в навигации'],
  ['line', '#dfe2e7', 'рамки карточек и панелей'],
  ['line-strong', '#c8ccd4', 'рамки полей и вторичных кнопок'],
  ['ink', '#1b202b', 'основной текст, главная кнопка'],
  ['ink-2', '#4d535e', 'второстепенный текст (7,7:1)'],
  ['ink-3', '#686d78', 'подписи и даты (5,1:1)'],
  ['accent', '#2661c2', 'ссылки, выбор, фокус, «новое»'],
  ['accent-soft', '#e9f0fd', 'выбранная строка, информационный бейдж'],
  ['good / good-soft', '#116b37', 'оценка от 70, письмо готово'],
  ['warn / warn-soft', '#88520c', 'оценка 50–69, «уточнить», внимание'],
  ['bad / bad-soft', '#b32926', 'ошибки, опасные действия'],
]

const TYPE: [string, string, string][] = [
  ['text-2xl font-semibold', '24 / 600', 'Заголовок экрана'],
  ['text-lg font-semibold', '18 / 600', 'Заголовок карточки вакансии'],
  ['text-prose', '15 / 400', 'Письмо и описание вакансии: длинный текст для чтения'],
  ['text-sm', '14 / 400', 'Основной текст интерфейса, строки списка, кнопки'],
  ['text-caption', '13 / 400', 'Вторая строка: компания · город · формат'],
  ['text-xs font-medium', '12 / 500', 'Заголовки колонок, бейджи, счётчики'],
]

const ROWS = [
  { name: 'Python-разработчик (по названию)', found: 64, sent: 9 },
  { name: 'Frontend с React в описании', found: 112, sent: 5 },
  { name: 'QA-автоматизация (по названию)', found: 27, sent: 2 },
]

/** The design system on one screen: tokens and every base component in its states. Not in the menu — open /ui when
 *  changing the interface; README «Дизайн-система» describes the rules. */
export default function DesignSystemPage() {
  const [tab, setTab] = useState('a')
  const [format, setFormat] = useState('REMOTE')
  const [exp, setExp] = useState(['between1And3'])
  const [sort, setSort] = useState('found')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [page, setPage] = useState(0)
  const [q, setQ] = useState('')
  const [sheet, setSheet] = useState(false)
  const [dialog, setDialog] = useState(false)
  const rows = [...ROWS].sort((a, b) => (order === 'asc' ? 1 : -1) * ((a[sort as 'found' | 'sent'] as number) - (b[sort as 'found' | 'sent'] as number)))
  const onSort = (id: string) => {
    if (sort === id) setOrder(order === 'asc' ? 'desc' : 'asc')
    else {
      setSort(id)
      setOrder('desc')
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Дизайн-система" meta="Токены и базовые компоненты интерфейса · шрифт Golos Text · иконки Lucide. Правила описаны в README, раздел «Дизайн-система»." />

      <Panel title="Цвета" meta="OKLCH: холодные нейтральные, один акцент (кобальт) и три смысловых пары. Контраст любого текста к своему фону не ниже 4,5:1.">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {COLORS.map(([name, hex, use]) => (
            <div key={name} className="flex items-center gap-3">
              <span className="size-10 shrink-0 rounded-md border border-line" style={{ background: hex }} />
              <span className="min-w-0 text-caption">
                <span className="block font-medium text-ink">
                  {name} <span className="font-normal tabular-nums text-ink-3">{hex}</span>
                </span>
                <span className="block text-ink-2">{use}</span>
              </span>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Шрифт и размеры" meta="Golos Text (кириллица и латиница, табличные цифры), шесть размеров на весь интерфейс.">
        <div className="space-y-2">
          {TYPE.map(([cls, size, sample]) => (
            <div key={size} className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-4">
              <span className="text-xs tabular-nums text-ink-3">{size}</span>
              <span className={cls}>{sample}</span>
            </div>
          ))}
          <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-4">
            <span className="text-xs text-ink-3">цифры</span>
            <span className="text-sm tabular-nums">1 111 ₽ · 200 000 ₽ · 2 859 · 30.09: цифры одинаковой ширины, колонки ровные</span>
          </div>
        </div>
      </Panel>

      <Panel title="Кнопки" meta="Одна главная (тёмная) на экран, вторичные с рамкой, текстовые для частых действий, красные только для опасных. Нажатие чуть уменьшает кнопку.">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary">Открыть отклик на hh.ru</Button>
            <Button>Пропустить</Button>
            <Button>
              <Clock aria-hidden />
              Отложить
            </Button>
            <Button variant="ghost">Отметить всё просмотренным</Button>
            <Button variant="danger">Скрыть работодателя</Button>
            <Button variant="ghost" iconOnly aria-label="Ещё действия">
              <MoreHorizontal aria-hidden />
            </Button>
            <Button variant="primary" disabled>
              Недоступно
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="primary">
              Маленькая главная
            </Button>
            <Button size="sm">
              <Link2 aria-hidden />
              По ссылке
            </Button>
            <Button size="sm" variant="ghost">
              Версии (3)
            </Button>
          </div>
        </div>
      </Panel>

      <Panel title="Бейджи, счётчики, чипы" meta="Бейдж показывает состояние (прямоугольный), чип показывает действующий фильтр с крестиком (круглый), переключатель выбирает несколько вариантов.">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="good">Письмо готово</Badge>
            <Badge tone="warn">Уточнить письмо</Badge>
            <Badge tone="info">В пакете Claude</Badge>
            <Badge tone="bad">Ошибка</Badge>
            <Badge>Пропущена</Badge>
            <Badge tone="outline">дубль</Badge>
            <span className="mx-2 h-5 w-px bg-line" />
            <Count value={83} attention />
            <Count value={14} />
            <Count value={3737} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Chip onRemove={() => {}} removeLabel="Убрать фильтр">
              Формат: удалённо
            </Chip>
            <Chip onRemove={() => {}} removeLabel="Убрать фильтр">
              Опыт: 1–3 года
            </Chip>
            <Button variant="ghost" size="sm">
              Сбросить все
            </Button>
            <span className="mx-2 h-5 w-px bg-line" />
            {[
              ['noExperience', 'без опыта'],
              ['between1And3', '1–3 года'],
              ['between3And6', '3–6 лет'],
            ].map(([id, label]) => (
              <ToggleChip key={id} pressed={exp.includes(id)} onToggle={() => setExp((e) => (e.includes(id) ? e.filter((x) => x !== id) : [...e, id]))}>
                {label}
              </ToggleChip>
            ))}
          </div>
          <Segmented
            label="Формат"
            value={format}
            onChange={setFormat}
            options={[
              ['', 'Любой'],
              ['REMOTE', 'Удалённо'],
              ['HYBRID', 'Гибрид'],
              ['ON_SITE', 'Офис'],
            ]}
          />
        </div>
      </Panel>

      <Panel title="Вкладки" meta="Подчёркнута текущая группа; счётчик с акцентом означает очередь, которая ждёт вас. Стрелки влево и вправо переключают.">
        <Tabs
          label="Пример вкладок"
          value={tab}
          onChange={setTab}
          items={[
            { id: 'a', label: 'Кандидаты', count: 42 },
            { id: 'b', label: 'Уточнить письмо', count: 6, attention: true },
            { id: 'c', label: 'Отправлено', count: 57 },
          ]}
        />
      </Panel>

      <Panel title="Поля" meta="Подпись над полем, подсказка под ним; высота 34 px (40 px на сенсорном экране); фокус обозначает синее кольцо, рамка не прыгает. Поле занимает всю строку, пока ему не задали свою ширину.">
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Регион" hint="Как на hh.ru: Москва">
              <Input placeholder="Москва" />
            </Field>
            <Field label="Поиск">
              <Select defaultValue="">
                <option value="">Все поиски</option>
                <option>Python-разработчик (по названию)</option>
              </Select>
            </Field>
            <Field label="Недоступное поле">
              <Input disabled value="только чтение" readOnly />
            </Field>
          </div>
          <SearchInput label="Поиск по названию" placeholder="Название или компания" value={q} onChange={setQ} />
          <Textarea placeholder="Многострочный текст: письмо, вопросы работодателя" />
          <Checkbox label="Только новые" defaultChecked />
        </div>
      </Panel>

      <Panel title="Блоки, таблицы, показатели" meta="Блок страницы: белая панель с заголовком; таблица прокручивается вбок внутри блока; цифры выровнены вправо и одинаковой ширины." flush>
        <div className="grid grid-cols-2 gap-3 px-4 pb-4 sm:px-5 lg:grid-cols-4">
          <Stat label="Найдено" value={187} hint="Вакансии, впервые найденные за период" />
          <Stat label="Отправлено" value={12} sub="API 0 · вручную 12" />
          <Stat label="Приглашений" value={2} sub="16,7% от отправленных" tone="good" />
          <Stat label="Осталось на сегодня" value="18 из 30" />
        </div>
        <Table label="Пример таблицы">
          <thead>
            <tr>
              <Th>Поиск</Th>
              <SortTh id="found" label="Найдено" sort={sort} order={order} onSort={onSort} align="right" />
              <SortTh id="sent" label="Отправлено" sort={sort} order={order} onSort={onSort} align="right" />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name}>
                <Td>{r.name}</Td>
                <Td align="right">{r.found}</Td>
                <Td align="right">{r.sent}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
        <Pagination page={page} perPage={50} total={137} onPage={setPage} />
      </Panel>

      <Panel title="Плотная таблица" meta="Для многих числовых колонок (сегменты и тренды в «Аналитике»): мельче шрифт и поля, числа в одну строку, длинные заголовки переносятся." flush>
        <Table label="Пример плотной таблицы" density="compact">
          <thead>
            <tr>
              <Th>Сегмент</Th>
              <Th align="right">Найдено</Th>
              <Th align="right">{'≥ 50'}</Th>
              <Th align="right">Отправлено</Th>
              <Th align="right">Google Sheets / Excel</Th>
            </tr>
          </thead>
          <tbody>
            {ROWS.map((r) => (
              <tr key={r.name}>
                <Td>{r.name}</Td>
                <Td align="right">{r.found}</Td>
                <Td align="right">
                  {Math.round(r.found * 0.4)} <span className="text-ink-3">40%</span>
                </Td>
                <Td align="right">{r.sent}</Td>
                <Td align="right">{Math.round(r.found / 7)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title="Описание и раскрывающийся текст">
        <div className="space-y-4">
          <Dl
            rows={[
              ['Состояние', <Badge tone="good">настроено</Badge>],
              ['Вечерняя сводка', 'ежедневно в 21:00'],
              ['Очередь', 'в очереди 0 · отправлено 214 · ошибок 0'],
            ]}
          />
          <Disclosure summary="Как это работает">
            <ul className="list-disc space-y-1 pl-5 text-caption text-ink-2">
              <li>Длинное пояснение по умолчанию свёрнуто, чтобы не отодвигать данные.</li>
              <li>Открывается мышью, клавишами Enter и пробелом.</li>
            </ul>
          </Disclosure>
        </div>
      </Panel>

      <Panel title="Оценка и этапы" meta="Оценку Claude показывает цвет полосы: от 70 зелёная, 50–69 жёлтая. Предварительная оценка в пунктирной рамке. Этапы показывают, где вакансия сейчас.">
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <ScoreChip score={82} prescore={45} />
            <ScoreChip score={64} prescore={40} />
            <ScoreChip score={38} prescore={40} />
            <ScoreChip score={null} prescore={45} />
            <ScoreChip score={null} prescore={null} />
          </div>
          <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
            <ScoreMeter score={80} prescore={43} verdict="Подходит" note="Claude, 13.10" />
            <ScoreMeter score={null} prescore={43} note="Claude ещё не разбирал" />
          </div>
          <PipelineTrack
            steps={[
              { key: '1', label: 'Найдена', state: 'done' },
              { key: '2', label: 'Отбор', state: 'done' },
              { key: '3', label: 'Оценка Claude', state: 'done' },
              { key: '4', label: 'Письмо', state: 'attention' },
              { key: '5', label: 'Отклик', state: 'todo' },
            ]}
          />
          <PipelineTrack
            steps={[
              { key: '1', label: 'Найдена', state: 'done' },
              { key: '2', label: 'Отбор', state: 'done' },
            ]}
            exit={{ label: 'Пропущена', tone: 'neutral' }}
          />
        </div>
      </Panel>

      <Panel title="Сообщения и состояния" meta="Сообщения стоят внутри страницы, рядом с тем, к чему относятся; у ошибки есть код и кнопка «Повторить»; пустой список объясняет, почему он пустой.">
        <div className="space-y-3">
          <Notice tone="info" title="Идёт переоценка по критериям">
            Список обновится сам, когда она закончится.
          </Notice>
          <Notice tone="good" title="В очередь поставлено: 3" />
          <Notice tone="warn" title="Уточните перед отправкой">
            [[УТОЧНИТЬ: опыт с RAG в продакшене]]
          </Notice>
          <ErrorNotice error={new ApiError(503, { detail: 'База занята: идёт скан' })} title="Не удалось загрузить вакансии" onRetry={() => {}} />
          <div className="rounded-lg border border-line">
            <EmptyState title="Ничего не найдено" action={<Button>Сбросить фильтры и поиск</Button>}>
              В группе «Кандидаты» 42 вакансии, но поиск и фильтры скрывают все.
            </EmptyState>
          </div>
          <div className="space-y-2">
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-3 w-1/3" />
          </div>
        </div>
      </Panel>

      <Panel
        title="Слои и движение"
        meta="Анимация только там, где она объясняет, откуда что появилось. Частые действия (вкладки, фильтры, J/K, навигация) срабатывают мгновенно. Esc закрывает верхний слой, фокус возвращается к кнопке, которая его открыла."
      >
        <div className="space-y-4">
          <ul className="list-disc space-y-1 pl-5 text-caption text-ink-2">
            <li>Боковая панель карточки: выезжает справа за 280 мс, уходит туда же за 180 мс; затемнение фона за 200 мс.</li>
            <li>Меню и панель фильтров: 150 мс, вырастают из своей кнопки. На телефоне панель выезжает снизу.</li>
            <li>Окно подтверждения: 200 мс из центра, без сдвига.</li>
            <li>Нажатие кнопки: уменьшение до 97 % за 150 мс.</li>
            <li>Если в системе включено «уменьшить движение», остаётся только плавное появление без сдвигов.</li>
          </ul>
          <div className="flex flex-wrap gap-2">
            <Popover
              label="Пример панели"
              trigger={(p) => (
                <Button {...p}>
                  <SlidersHorizontal aria-hidden />
                  Фильтры
                </Button>
              )}
            >
              {(close) => (
                <div className="space-y-3">
                  <Field label="Регион">
                    <Input data-autofocus placeholder="Москва" />
                  </Field>
                  <Button variant="primary" onClick={close}>
                    Готово
                  </Button>
                </div>
              )}
            </Popover>
            <Menu
              label="Пример меню"
              items={[{ section: 'Сортировка' }, { key: 'score', label: 'По оценке', checked: true }, { key: 'fresh', label: 'Сначала свежие', checked: false }, 'separator', { key: 'hide', label: 'Скрыть', tone: 'danger' }]}
              trigger={(p) => <Button {...p}>Меню</Button>}
            />
            <Button onClick={() => setSheet(true)}>Боковая панель</Button>
            <Button onClick={() => setDialog(true)}>Окно подтверждения</Button>
          </div>
        </div>
      </Panel>

      {sheet && (
        <Sheet size="md" labelledBy="ds-sheet-title" onClose={() => setSheet(false)}>
          <div className="flex h-12 shrink-0 items-center justify-between border-b border-line pl-5 pr-3">
            <h2 id="ds-sheet-title" className="text-[15px] font-semibold">
              Боковая панель
            </h2>
            <SheetCloseButton />
          </div>
          <div className="space-y-3 px-5 py-5 text-sm text-ink-2">
            <p>Карточки вакансии, отклика, тендера и студии открываются так. Фокус внутри панели, Tab по кругу, Esc закрывает.</p>
            <Button onClick={() => setDialog(true)}>Открыть окно поверх</Button>
          </div>
        </Sheet>
      )}
      {dialog && (
        <Dialog labelledBy="ds-dialog-title" onClose={() => setDialog(false)}>
          {(close) => (
            <>
              <h2 id="ds-dialog-title" className="text-lg font-semibold">
                Окно подтверждения
              </h2>
              <p className="mt-2 text-sm text-ink-2">Только для решений, которые останавливают всё остальное: согласие перед первым откликом.</p>
              <div className="mt-5 flex justify-end gap-2">
                <Button variant="ghost" onClick={close}>
                  Отмена
                </Button>
                <Button variant="primary" onClick={close}>
                  Подтверждаю
                </Button>
              </div>
            </>
          )}
        </Dialog>
      )}
    </div>
  )
}
