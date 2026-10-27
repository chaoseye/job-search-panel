export interface HHUser {
  id: string | null
  first_name: string | null
  last_name: string | null
  email: string | null
  is_applicant: boolean
  is_employer: boolean
  auth_type: string | null
  counters: Record<string, number> | null
}

export interface AuthStatus {
  logged_in: boolean
  state: 'none' | 'ok' | 'expired' | 'revoked' | string
  expires_at: string | null
  hh_user_id: string | null
  user: HHUser | null
  client_id_set: boolean
  redirect_uri: string
  user_agent: string
  user_agent_placeholder: boolean
}

export type Capability = 'available' | 'unavailable' | 'unknown' | 'skipped'

export interface ProbeStep {
  key: string
  title: string
  method: string
  path: string
  status: number | null
  capability: Capability
  note: string
  errors: { type?: string; value?: string }[]
  duration_ms: number
}

export interface ProbeReport {
  started_at: string
  finished_at: string
  steps: ProbeStep[]
  capabilities: Record<string, Capability>
  apply_mode_suggested: 'api' | 'assist'
  apply_mode_effective?: 'api' | 'assist'
  resume_ids: { id: string; title: string; status?: string }[]
  me: { first_name?: string | null; last_name?: string | null; is_applicant?: boolean } | null
}

export interface SettingsView {
  apply_mode: 'api' | 'assist'
  apply_mode_override: 'auto' | 'api' | 'assist'
  apply_mode_env: string
  capabilities: Record<string, Capability> | null
  probe_at: string | null
  paused_reason: string | null
  captcha_url: string | null
  resume_id: string | null
  resumes_hint: { id: string; title: string; status?: string }[]
  hh: { client_id_set: boolean; redirect_uri: string; user_agent: string; rps: number }
  limits: {
    apply_max_per_day: number
    apply_max_per_hour: number
    apply_window: string
    apply_autopilot: boolean
    apply_dry_run: boolean
  }
  telegram_configured: boolean
}

export interface HealthView {
  ok: boolean
  version: string
  auth: { logged_in: boolean; state: string; expires_at: string | null }
  paused_reason: string | null
  apply_mode: string
  scheduler_jobs: string[]
  last_jobs: { job: string; status: string; started_at: string; error: string | null }[]
}

// ---- profile (M1) ----

export interface ResumeRow {
  id: number
  source: 'hh' | 'file'
  title: string | null
  hh_resume_id: string | null
  file_name: string | null
  is_primary: boolean
  chars: number
  updated_at: string | null
  hh_updated_at: string | null
  can_publish_or_update: boolean | null
  next_publish_at: string | null
  alternate_url: string | null
}

export interface ExperienceItem {
  company: string | null
  position: string | null
  start: string | null
  end: string | null
  description: string | null
  industries: string[]
  area: string | null
}

export interface EducationItem {
  kind: string
  name: string | null
  organization: string | null
  result: string | null
  year: number | null
}

export interface NormalizedResume {
  hh_id: string | null
  title: string | null
  full_name: string | null
  age: number | null
  area: string | null
  salary_amount: number | null
  salary_currency: string | null
  total_experience_months: number | null
  skills: string[]
  about: string | null
  experience: ExperienceItem[]
  education_level: string | null
  education: EducationItem[]
  languages: string[]
  professional_roles: string[]
  relocation: string | null
  business_trips: string | null
  work_formats: string[]
  employment_forms: string[]
  certificates: string[]
  sites: string[]
  citizenship: string[]
  status: string | null
  updated_at: string | null
  alternate_url: string | null
}

export interface SkillWeight {
  name: string
  weight: number
}

export interface LetterStyle {
  tone: string
  min_chars: number
  max_chars: number
  greeting: string
  must_mention: string[]
  avoid: string[]
  examples: string[]
}

export interface Preferences {
  target_titles: string[]
  target_keywords: string[]
  stop_words_title: string[]
  stop_words_description: string[]
  salary_min: number | null
  salary_currency: string
  salary_missing: 'keep' | 'skip'
  work_formats: string[]
  areas: string[]
  employment_forms: string[]
  relocation: boolean
  business_trips: 'never' | 'rarely' | 'ready'
  excluded_employers: string[]
  excluded_industries: string[]
  experience_min_years: number | null
  experience_max_years: number | null
  own_experience_years: number | null
  languages: string[]
  no_agencies: boolean
  accredited_it_only: boolean
  wants: string
  avoids: string
  extra_facts: string
  letter_style: LetterStyle
  skills_weights: SkillWeight[]
}

export interface ProfileView {
  version: number | null
  generated_at: string | null
  hash: string | null
  profile_md_path: string
  profile_md_exists: boolean
  profile_md_chars: number
  set_profiles: { profile_id: number; resume_id: number; resume_name: string | null; file: string; exists: boolean }[]
  resumes: ResumeRow[]
  structured: NormalizedResume | null
  file: { name: string | null; chars: number; preview: string } | null
  preferences: Preferences
  capabilities: Record<string, Capability | null>
  manual_resume_id: string | null
  reprocess_started?: boolean
  resumes_hint: { id: string; title: string; status?: string }[]
  hh_logged_in: boolean
  warnings: string[]
}

export interface HHResumeListItem {
  id: string
  title: string
  status: string
  updated_at: string | null
  alternate_url: string | null
  can_publish_or_update: boolean | null
  next_publish_at: string | null
  total_views: number | null
  new_views: number | null
}

// ---- searches & vacancies (M2) ----

export interface Ref {
  id: string
  name: string
}

export interface SearchParams {
  text: string
  search_field: string[]
  experience: string[]
  employment_form: string[]
  work_format: string[]
  work_schedule_by_days: string[]
  salary: number | null
  currency: string
  only_with_salary: boolean
  label: string[]
  professional_role: Ref[]
  industry: Ref[]
  areas: Ref[]
  excluded_text: string
  education: string[]
  period_days: number | null
}

export interface SavedSearch {
  id: number
  name: string
  source: 'search' | 'similar_to_resume'
  params: SearchParams
  summary: string
  interval_min: number
  enabled: boolean
  /** tracking only: new vacancies go to the bot as links (event search_new), nothing is evaluated */
  watch_only: boolean
  selection_profile_id: number | null
  selection_profile_name: string | null
  last_run_at: string | null
  last_seen_published_at: string | null
  last_found: number | null
  last_new: number | null
  last_requests: number | null
  last_error: string | null
  last_warning: string | null
  created_at: string | null
}

export interface SearchInput {
  name: string
  source: 'search' | 'similar_to_resume'
  params: SearchParams
  interval_min: number
  enabled: boolean
  watch_only?: boolean
  selection_profile_id?: number | null
}

/** Named set of vacancy selection criteria (subset of Preferences); the profile and letters stay shared. */
export interface SelectionProfile {
  id: number
  name: string
  criteria: Preferences
  searches: number
  vacancies: number
  resume_id: number | null
  resume_name: string | null
  profile_file: string | null
  updated_at: string | null
}

export interface AddByLinksResult {
  added: { id: number; hh_id: string; name: string; employer: string | null; prescore: number | null; status: string; created: boolean; url: string | null }[]
  errors: { url: string; error: string }[]
  batch_ids: string[]
}

export interface Option {
  id: string
  name: string
}

export interface SearchOptions {
  experience: Option[]
  employment_form: Option[]
  work_format: Option[]
  work_schedule_by_days: Option[]
  vacancy_label: Option[]
  search_field: Option[]
  currency: Option[]
  education: Option[]
  industry: Option[]
  similar_available: boolean
  resume_id: string | null
}

export interface PreviewResult {
  found: number | null
  pages: number | null
  items: {
    hh_id: string
    name: string
    employer_name: string | null
    area_name: string | null
    salary_from: number | null
    salary_to: number | null
    salary_currency: string | null
    published_at: string | null
    alternate_url: string | null
    responses_count: number | null
  }[]
}

export interface VacancyRow {
  id: number
  hh_id: string
  name: string
  employer: { id: string | null; name: string | null; accredited_it: boolean | null; trusted: boolean | null }
  area_name: string | null
  // workplace: street with the building and metro stations, the nearest first (empty for remote / no address)
  address: string | null
  metro: string[]
  salary: {
    from: number | null
    to: number | null
    currency: string | null
    gross: boolean | null
    net_estimate: number | null
    net_rur: number | null
  }
  experience_name: string | null
  employment_form_name: string | null
  work_formats: string[]
  work_format_ids: string[]
  published_at: string | null
  first_seen_at: string | null
  responses_count: number | null
  duplicate_of_id: number | null
  sources: string[]
  flags: string[]
  pipeline_status: string
  status_label: string
  pipeline_reason: string | null
  prescore: number | null
  score: number | null
  verdict: 'apply' | 'maybe' | 'skip' | null
  letter_status: 'none' | 'ready' | 'edited' | 'template' | 'needs_clarification'
  batch_id: string | null
  selection_profile_id: number | null
  selection_profile_name: string | null
  alternate_url: string | null
  apply_alternate_url: string | null
  is_new: boolean
  snoozed_until: string | null
  hidden: boolean
  user_override: boolean
  archived: boolean
  has_full: boolean
}

export interface VacancyCounts {
  by_status: Record<string, number>
  candidates: number
  suitable: number
  maybe: number
  prescored: number
  awaiting_claude: number
  needs_manual: number
  not_suitable: number
  filtered_out: number
  skipped: number
  hidden: number
  already_applied: number
  new: number
  snoozed: number
  unseen_candidates: number
  needs_clarification: number
  total: number
}

export interface VacancyList {
  items: VacancyRow[]
  total: number
  page: number
  per_page: number
  counts: VacancyCounts
}

export interface Evaluation {
  id: number
  batch_id: string
  score: number
  verdict: 'apply' | 'maybe' | 'skip'
  summary: string
  matched: string[]
  gaps: string[]
  red_flags: string[]
  salary_fit: string | null
  seniority_fit: string | null
  language: string | null
  profile_version: number | null
  stale_profile: boolean
  user_feedback: 'ok' | 'wrong' | null
  user_comment: string | null
  created_at: string | null
}

export interface LetterView {
  id: number
  version: number
  text: string
  language: string | null
  char_count: number
  origin: 'claude' | 'edited' | 'template'
  batch_id: string | null
  mentions: string[]
  needs_clarification: string[]
  warnings: string[]
  is_current: boolean
  status: string
  created_at: string | null
  send_problems: string[]
}

export interface LetterVersion {
  id: number
  version: number
  origin: string
  char_count: number
  is_current: boolean
  created_at: string | null
}

export interface RevisionRequest {
  id: number
  status: 'queued' | 'sent_in_batch' | 'done' | 'cancelled'
  comment: string
  batch_id: string | null
}

export interface ClaudeBatch {
  id: number
  batch_id: string
  status: 'pending' | 'imported' | 'partial' | 'rejected' | 'cancelled'
  tasks_count: number
  results_count: number
  profile_version: number | null
  packet_path: string
  packet_exists: boolean
  result_path: string | null
  created_at: string | null
  imported_at: string | null
  error: string | null
  summary: Record<string, unknown> | null
  tasks?: { type: string; vacancy_id: string | null; name: string | null }[]
}

export interface BatchesSummary {
  waiting_for_packet: number
  prescored: number
  revision_requests: number
  awaiting_claude: number
  pending_batches: number
  pending_files: string[]
  done_files: string[]
  exchange_dir: string
  profile_version: number | null
  guidelines_exists: boolean
  batch_size: number
  auto_create: boolean
  command: string
}

export interface ImportOutcome {
  file: string
  batch_id: string | null
  status: 'imported' | 'partial' | 'rejected' | 'skipped'
  evaluations: number
  letters: number
  errors: string[]
  warnings: string[]
  missing: string[]
  stale_profile: boolean
}

export type VacancyTags = { domain: string[]; tasks: string[]; stack: string[] }

export interface QuestionAnswersView {
  id: number
  vacancy_id: number
  questions: string[]
  status: 'queued' | 'sent_in_batch' | 'done' | 'cancelled'
  status_label: string
  batch_id: string | null
  result: { answers: { question: string; answer: string; needs_clarification: string[] }[] } | null
  error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface VacancyDetail extends VacancyRow {
  evaluation: Evaluation | null
  letter: LetterView | null
  letters: LetterVersion[]
  revision_request: RevisionRequest | null
  description_html: string | null
  description_text: string | null
  key_skills: { name: string; matched: boolean }[]
  professional_roles: string[]
  tags: VacancyTags
  question_answers: QuestionAnswersView[]
  rules_reasons: string[]
  prescore_notes: string[]
  relations: string[]
  response_letter_required: boolean
  has_test: boolean
  test_required: boolean | null
  type_id: string | null
  response_url: string | null
  full_fetched_at: string | null
  skip_reason: string | null
  history: { ts: string | null; level: string; area: string; message: string }[]
  affected?: number
}

export interface JobRun {
  id: number
  job: string
  status: 'running' | 'ok' | 'error'
  started_at: string | null
  finished_at: string | null
  duration_sec: number | null
  stats: Record<string, unknown> | null
  error: string | null
}

export interface JobsView {
  running: string[]
  runs: JobRun[]
  last_scan_at: string | null
  last_sync_at: string | null
  paused_reason: string | null
  failures: Record<string, number>
}

export interface HiddenEmployer {
  id: number
  employer_id: string | null
  employer_name: string | null
  created_at: string | null
}

// ---- applications (M4) ----

export interface LimitsStatus {
  today_sent: number
  max_per_day: number
  remaining_day: number
  hour_sent: number
  max_per_hour: number
  remaining_hour: number
  window: string
  in_window: boolean
  paused_until: string | null
  paused_reason: string | null
  hh_cap: number
  can_send: boolean
  blocked_reasons: string[]
}

export interface ApplyStatus {
  apply_mode: 'api' | 'assist'
  consent_accepted_at: string | null
  resume_id: string | null
  dry_run: boolean
  autopilot: boolean
  autopilot_min_score: number
  limits: LimitsStatus
  job_running: boolean
}

export interface ApplicationRow {
  id: number
  vacancy_id: number
  vacancy: {
    id: number
    hh_id: string
    name: string
    employer_name: string | null
    alternate_url: string | null
    apply_alternate_url: string | null
    pipeline_status: string
    score: number | null
  } | null
  mode: 'api' | 'assist' | 'external'
  mode_label: string
  status: 'queued' | 'sending' | 'sent' | 'failed' | 'needs_manual' | 'cancelled'
  status_label: string
  letter_text: string | null
  queued_at: string | null
  sent_at: string | null
  hh_negotiation_id: string | null
  negotiation_url: string | null
  response_url: string | null
  error_type: string | null
  error_value: string | null
  error_text: string | null
  attempts: number
  confirmed_by_user: boolean
  dry_run: boolean
  autopilot: boolean
  created_at: string | null
  apply_url?: string | null
  negotiation: NegotiationView | null
}

export interface ApplicationList {
  items: ApplicationRow[]
  total: number
  page: number
  per_page: number
  counts: Record<string, number>
  stage_counts: Record<string, number>
}

// ---- the board «Воронка» ----

export type BoardColumnKey = 'new' | 'confirm' | 'sent' | 'viewed' | 'answer'
// «Новые»: Claude's verdict, letters with a question, rejected by Claude, not evaluated yet, a step on hh.ru
export type BoardBand = 'hi' | 'mid' | 'clarify' | 'low' | 'wait' | 'manual'

export interface BoardItem {
  vacancy: VacancyRow
  // the response for the columns past «Новые» (with its negotiation), null in «Новые»
  application: ApplicationRow | null
  // when the card came to its column: found, opened on hh.ru, sent, viewed, answered
  stage_at: string | null
  outcome: 'invitation' | 'discard' | 'message' | null
}

export interface BoardColumn {
  key: BoardColumnKey
  title: string
  count: number
  items: BoardItem[]
}

export interface BoardNewCounts {
  hi: number
  mid: number
  low: number
  wait: number
  manual: number
  fresh_today: number
  clarify: number
  snoozed: number
}

export interface BoardFunnelStep {
  key: string
  label: string
  value: number
  hint: string | null
}

export interface BoardView {
  thresholds: { suitable: number; maybe: number }
  funnel: BoardFunnelStep[]
  new: BoardNewCounts
  columns: BoardColumn[]
}

export interface BoardColumnList {
  key: BoardColumnKey
  band: BoardBand | null
  total: number
  offset: number
  items: BoardItem[]
}

// the boards of the other pipelines, switched over the board: tenders of workspace.ru and studios
export type BoardSource = 'vacancies' | 'tenders' | 'studios'

export type TenderBoardColumnKey = 'new' | 'ready' | 'responded' | 'answer'
// «Найдено»: the open tenders that suit the questionnaire and the watched sections, and the other open ones
export type TenderBoardBand = 'relevant' | 'other'

export interface TenderBoardItem {
  tender: TenderRow
  // when the tender came to its column: found, its answer written or asked for, responded, the organizer's answer
  // recorded
  stage_at: string | null
}

export interface TenderBoardView {
  source: 'tenders'
  funnel: BoardFunnelStep[]
  bands: Record<TenderBoardBand, number>
  // suitable open tenders found today (local day)
  fresh_today: number
  columns: { key: TenderBoardColumnKey; title: string; count: number; items: TenderBoardItem[] }[]
}

export type StudioBoardColumnKey = 'new' | 'ready' | 'sent' | 'answer'
// «Найдено» by the contacts: found on the site or typed in, the site not read yet, nothing found
export type StudioBoardBand = 'contacts' | 'unchecked' | 'nocontacts'

export interface StudioBoardItem {
  studio: StudioRow
  // when the studio came to its column: found, its letter written or asked for, sent, answered
  stage_at: string | null
}

export interface StudioBoardView {
  source: 'studios'
  funnel: BoardFunnelStep[]
  bands: Record<StudioBoardBand, number>
  sent_today: number
  daily_limit: number
  columns: { key: StudioBoardColumnKey; title: string; count: number; items: StudioBoardItem[] }[]
}

/** A page of an open column of the tenders or the studios board. */
export interface SourceColumnList<K extends string, B extends string, I> {
  source: BoardSource
  key: K
  band: B | null
  total: number
  offset: number
  items: I[]
}

// ---- negotiations / sync (M5) ----

export interface NegotiationView {
  hh_id: string
  state_id: 'response' | 'invitation' | 'discard' | 'hidden' | null
  state_label: string
  hidden: boolean
  viewed_by_opponent: boolean
  has_updates: boolean
  messaging_status: string | null
  unread_count: number
  messages_count: number
  chat_id: string | null
  hh_created_at: string | null
  hh_updated_at: string | null
  first_viewed_at: string | null
  first_reaction_at: string | null
  invited_at: string | null
  discarded_at: string | null
  last_employer_message_at: string | null
  messages_synced_at: string | null
  last_synced_at: string | null
  manual: boolean
  page_url: string | null
}

export interface MessageView {
  id: number
  hh_message_id: string
  author_type: 'applicant' | 'employer' | 'bot' | 'unknown'
  author_name: string | null
  text: string | null
  created_at: string | null
  viewed_by_me: boolean | null
  viewed_by_opponent: boolean | null
  source: string
}

export interface NegotiationEventView {
  id: number
  event_type: 'created' | 'viewed' | 'state_changed' | 'message'
  label: string
  from_state: string | null
  to_state: string | null
  to_state_label: string
  occurred_at: string | null
  payload: Record<string, unknown> | null
}

export interface ReplyDraftView {
  id: number
  status: 'queued' | 'sent_in_batch' | 'done' | 'cancelled'
  status_label: string
  user_intent: string | null
  batch_id: string | null
  text: string | null
  intent: string | null
  needs_clarification: string[]
  error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface NegotiationDetail {
  negotiation: NegotiationView
  messages: MessageView[]
  events: NegotiationEventView[]
  reply_drafts: ReplyDraftView[]
  application_id: number | null
}

// ---- maintenance (M6) ----

export interface BackupsView {
  items: { name: string; size_bytes: number; created_at: string }[]
  dir: string
  keep: number
  daily_at: string
  db_path: string | null
}

export interface ClaudeStatus {
  enabled: boolean
  executable: string | null
  available: boolean
  pending: string[]
  timeout_sec: number
  tick_min: number
  cwd: string
  job_running: boolean
}

export interface SyncStatus {
  last_sync_at: string | null
  capability: Capability
  chats_capability: Capability
  negotiations: number
  with_updates: number
  job_running: boolean
  interval_min: number
}

// ---- stats (M5) ----

export interface StatsMetrics {
  found: number
  passed_rules: number
  evaluated: number
  suitable: number
  letters_ready: number
  sent: number
  sent_api: number
  sent_assist: number
  sent_external: number
  viewed: number
  invitations: number
  discards: number
  no_answer: number
  conv_sent_viewed: number | null
  conv_sent_invited: number | null
  conv_viewed_invited: number | null
  median_reaction_hours: number | null
  remaining_day: number
  max_per_day: number
  batches_created: number
  batches_imported: number
  letters_imported: number
  awaiting_claude: number
  hh_requests: number
}

export interface StatsSeriesDay {
  date: string
  found: number
  suitable: number
  sent: number
  viewed: number
  invited: number
  discarded: number
}

export interface BreakdownRow {
  label: string
  sent: number
  viewed: number
  invited: number
  discarded: number
  invited_pct: number | null
  viewed_pct: number | null
}

export interface StatsView {
  period: { key: string; date_from: string; date_to: string }
  generated_at: string
  no_answer_days: number
  metrics: StatsMetrics
  funnel: { stage: string; label: string; value: number }[]
  series: StatsSeriesDay[]
  score_buckets: { bucket: string; sent: number; invited: number; invited_pct: number | null }[]
  breakdowns: {
    by_search: BreakdownRow[]
    by_employer: BreakdownRow[]
    by_salary: BreakdownRow[]
    by_work_format: BreakdownRow[]
    by_area: BreakdownRow[]
    by_weekday_hour: {
      cells: { weekday: number; hour: number; sent: number; invited: number }[]
      by_weekday: { weekday: number; label: string; sent: number; invited: number }[]
      by_hour: { hour: number; sent: number; invited: number }[]
    }
  }
  counters: { date: string; applies_api: number; applies_assist_confirmed: number; applies_external: number; hh_requests: number; batches_created: number; letters_imported: number }[]
}

// ---- notifications / telegram (M5) ----

export interface NotificationRow {
  id: number
  channel: 'ui' | 'telegram'
  event_type: string
  label: string
  summary: string
  payload: Record<string, unknown>
  status: 'queued' | 'sent' | 'failed' | 'suppressed'
  attempts: number
  error: string | null
  text: string | null
  created_at: string | null
  sent_at: string | null
}

export interface DeliverySummary {
  days: number
  failed: number
  queued: number
  last_failed: { event_type: string; label: string; error: string | null; created_at: string | null } | null
}

export interface TelegramStatus {
  token_set: boolean
  chat_id: string | null
  chat_id_source: 'settings' | 'env' | null
  configured: boolean
  bot_username: string | null
  bot_name: string | null
  bot_link: string | null
  daily_summary_at: string
  resume_bump: { at: string[]; days: string; label: string | null; event_enabled: boolean }
  hot_digest_at: string | null
  letters: { enabled: boolean; min_score: number; event_in_env: boolean }
  /** vacancies with these words in the title do not go to the bot (they stay in the panel) */
  stop_words: string[]
  events: { id: string; label: string; enabled: boolean }[]
  queue: Record<string, number>
  last_sent_at: string | null
}

// ---- analytics (page «Аналитика») ----

export interface SegmentMetrics {
  found: number
  active: number
  evaluated: number
  ge50: number
  ge70: number
  ge50_pct: number | null
  ge70_pct: number | null
  awaiting: number
  salary_n: number
  salary_p25: number | null
  salary_median: number | null
  salary_p75: number | null
  no_salary_pct: number | null
  remote_pct: number | null
  hybrid_pct: number | null
  office_pct: number | null
  test_pct: number | null
  direct_pct: number | null
  letter_required_pct: number | null
  accredited_pct: number | null
  experience: Record<string, number>
  responses_median: number | null
  low_competition_pct: number | null
  sent: number
  viewed: number
  invited: number
  discarded: number
  reacted: number
  invited_pct: number | null
  reacted_pct: number | null
}

export interface SegmentRow extends SegmentMetrics {
  key: string
  label: string
}

export interface SegmentsView {
  by: string
  label: string
  days: number
  total: SegmentRow
  items: SegmentRow[]
}

export interface HotItem {
  id: number
  hh_id: string
  name: string
  employer: string | null
  url: string | null
  score: number | null
  verdict: string | null
  prescore: number | null
  responses: number
  published_at: string
  age_hours: number
  salary: number | null
  formats: string[]
  status: string
  has_test: boolean
}

export interface HotView {
  max_responses: number
  max_age_days: number
  min_score: number
  total: number
  items: HotItem[]
}

export interface SkillsView {
  found: number
  with_skills: number
  evaluated: number
  skills: { name: string; count: number; share: number | null; in_resume: boolean }[]
  missing: { name: string; count: number; share: number | null; in_resume: boolean }[]
  unused: string[]
  gaps: { text: string; count: number }[]
  gap_skills: { name: string; count: number }[]
  red_flags: { text: string; count: number }[]
  matched: { text: string; count: number }[]
}

export interface SalaryStats {
  label?: string
  n: number
  of: number
  p25: number | null
  median: number | null
  p75: number | null
}

export interface SalaryView {
  overall: SalaryStats
  by_format: SalaryStats[]
  by_experience: SalaryStats[]
  by_score: SalaryStats[]
  buckets: Record<string, number>
  mine: { expected: number | null; at_or_above: number; at_or_above_pct: number | null }
  salary_fit: Record<string, number>
  seniority_fit: Record<string, number>
  top_paid: { id: number; name: string; employer: string | null; salary: number; score: number | null; url: string | null }[]
}

export interface FunnelCell {
  sent: number
  viewed: number
  invited: number
  discarded: number
  reacted: number
  no_answer: number
  pending: number
  viewed_pct: number | null
  invited_pct: number | null
  reacted_pct: number | null
}

export interface FunnelView {
  by: string
  label: string
  overall: FunnelCell
  rows: (FunnelCell & { label: string })[]
  enough_data: boolean
}

export interface EmployerRow {
  employer_id: string | null
  name: string
  url: string | null
  vacancies: number
  active: number
  accredited: boolean | null
  trusted: boolean | null
  hidden: boolean
  first_seen: string | null
  last_published: string | null
  republished: number
  responses_median: number | null
  best_score: number | null
  sent: number
  viewed: number
  invited: number
  discarded: number
  reacted: number
  items: { id: number; name: string; url: string | null; score: number | null; prescore: number | null; status: string; salary: number | null; responses: number | null; published_at: string | null; archived: boolean }[]
}

export interface RecommendationsView {
  scope: string
  evaluated: number
  good: number
  enough_data: boolean
  searches: { term: string; query: string; evaluated: number; good: number; rate: number; in_base: number }[]
  stop_words: { term: string; evaluated: number; good: number; rate: number }[]
  weights: { name: string; in_good: number; in_all: number; weight: number | null; suggested: number }[]
  resume: { name: string; in_good: number; in_all: number }[]
  gaps: { name: string; count: number }[]
  focus: { by: string; by_label: string; key: string; label: string; found: number; evaluated: number; ge50_pct: number | null; responses_median: number | null; salary_median: number | null }[]
}

export interface TrendsView {
  weeks: { week: string; new: number; evaluated: number; ge50: number; sent: number; salary_median: number | null; responses_median: number | null; tools: Record<string, number> }[]
  tools: string[]
  tags: Record<'domain' | 'tasks' | 'stack', { name: string; count: number; share: number | null }[]>
}

export interface MailStatus {
  configured: boolean
  host: string | null
  user: string | null
  folder: string
  interval_min: number
  since_days: number
  sender_filter: string
  last: { at?: string; error?: string | null; summary?: { fetched: number; matched: number; unmatched: number; ignored: number; duplicates: number; last_uid: number | null } }
  unmatched: { subject: string; kind: string; at?: string; error?: string }[]
  interview_prep_auto: boolean
}

export interface InterviewPrepView {
  id: number
  application_id: number
  status: 'queued' | 'sent_in_batch' | 'done' | 'cancelled'
  status_label: string
  auto: boolean
  user_notes: string | null
  batch_id: string | null
  result: { summary: string; questions: { question: string; answer: string }[]; ask_them: string[]; risks: string[]; prepare: string[] } | null
  error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ResumeAdviceView {
  id: number
  segment_by: string | null
  segment_key: string | null
  segment_label: string
  user_notes: string | null
  demand: { found: number; evaluated: number; good: number; missing: { name: string; count: number }[]; gap_skills: { name: string; count: number }[] } | null
  status: 'queued' | 'sent_in_batch' | 'done' | 'cancelled'
  status_label: string
  batch_id: string | null
  result: { summary: string; edits: { section: string; proposed: string; reason: string }[]; add_skills: string[]; soften: string[]; questions: string[] } | null
  error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface BorderlineView {
  prescore_min: number
  score_min: number
  prescore_margin: number
  score_margin: number
  total: number
  items: { kind: 'prescore' | 'score'; value: number; threshold: number; id: number; hh_id: string; name: string; employer: string | null; url: string | null; status: string; salary: number | null; responses: number | null; published_at: string | null; formats: string[]; summary: string | null; gaps: string[] }[]
}

export interface QueueResult {
  queued: number[]
  rejected: { vacancy_id: number; name?: string; reason: string }[]
  job_started: boolean
  status: ApplyStatus
}

export interface CheckResult {
  items: { vacancy_id: number; name?: string; ok: boolean; problems: string[] }[]
  status: ApplyStatus
}

export interface AuditRow {
  id: number
  ts: string
  level: string
  area: string
  message: string
  context: string | null
}

// ---- Telegram channels with vacancies -------------------------------------------------------

export interface TelegramChannel {
  id: number
  username: string
  url: string
  title: string | null
  enabled: boolean
  mode: 'vacancies' | 'all'
  keywords: string | null
  notify: boolean
  last_message_id: number | null
  last_checked_at: string | null
  last_new: number | null
  last_error: string | null
  posts_count: number
  new_posts: number
  created_at: string | null
}

export interface ChannelsView {
  items: TelegramChannel[]
  interval_min: number
  languages: string[]
  running: boolean
}

export interface ChannelsBulkResult {
  added: TelegramChannel[]
  errors: { input: string; error: string }[]
}

export interface ChannelsSummary {
  channels: number
  enabled: number
  new_posts: number
  last_checked_at: string | null
  interval_min: number
  languages: string[]
}

export interface ChannelPost {
  id: number
  channel_id: number
  channel: string | null
  username: string
  message_id: number
  url: string | null
  posted_at: string | null
  title: string
  text: string
  html: string | null
  links: string[]
  hh_links: string[]
  is_vacancy: boolean
  lang: string | null
  status: 'new' | 'seen' | 'hidden'
  vacancy_id: number | null
  vacancy_status: string | null
  vacancy_status_label: string | null
  notified_at: string | null
  seen_at: string | null
  created_at: string | null
}

export interface ChannelPostList {
  items: ChannelPost[]
  total: number
  page: number
  per_page: number
  new_count: number
}

export interface ToPanelResult extends AddByLinksResult {
  post: ChannelPost
}

// ---- company career sites --------------------------------------------------------------------

export interface CompanySite {
  key: string
  name: string
  url: string
  note: string
  enabled: boolean
  notify: boolean
  keywords: string | null
  last_checked_at: string | null
  last_new: number | null
  last_error: string | null
  vacancies_count: number
  matches: number
  new_matches: number
  first_synced_at: string | null
}

export interface SitesView {
  items: CompanySite[]
  interval_min: number
  cities: string[]
  unsupported: Record<string, string>
  running: boolean
}

export interface SitesSummary {
  sites: number
  enabled: number
  new_matches: number
  last_checked_at: string | null
  interval_min: number
  cities: string[]
}

export interface SiteVacancy {
  id: number
  site_key: string
  site: string
  external_id: string
  title: string
  url: string
  company: string | null
  location: string | null
  work_format: string | null
  remote: boolean
  snippet: string | null
  published_at: string | null
  is_match: boolean
  status: 'new' | 'seen' | 'hidden' | 'gone'
  first_seen_at: string
  last_seen_at: string | null
  notified_at: string | null
}

export interface SiteVacancyList {
  items: SiteVacancy[]
  total: number
  page: number
  per_page: number
  new_count: number
}

// ---- studios ----

export type StudioStatus = 'new' | 'ready' | 'no_contacts' | 'drafting' | 'drafted' | 'sent' | 'replied' | 'declined' | 'skipped' | 'hidden'
export type StudioLetterKind = 'application' | 'offer'

export interface StudioRow {
  id: number
  name: string
  site_url: string | null
  domain: string | null
  hh_employer_id: string | null
  hh_url: string | null
  area: string | null
  kind: 'dev' | 'design' | 'mixed' | 'unknown'
  kind_label: string
  source: string
  sources: string[]
  letter_kind: StudioLetterKind
  offer_allowed: boolean
  open_vacancies: number | null
  services: string[]
  emails: string[]
  telegram: string | null
  contact_url: string | null
  contacts_checked_at: string | null
  contacts_error: string | null
  fit_score: number | null
  fit_notes: string[]
  status: StudioStatus
  status_label: string
  sent_at: string | null
  sent_to: string | null
  sent_via: string | null
  replied_at: string | null
  reply_subject: string | null
  first_seen_at: string | null
  notes: string | null
}

export type StudioLetterStyle = 'business' | 'simple'

export interface StudioLetterView {
  id: number
  version: number
  kind: StudioLetterKind
  /** Claude writes a draft per style, the user picks one */
  style: StudioLetterStyle
  style_label: string
  subject: string
  text: string
  full_text: string
  origin: 'claude' | 'edited'
  batch_id: string | null
  needs_clarification: string[]
  warnings: string[]
  is_current: boolean
  created_at: string | null
}

export interface StudioDetail extends StudioRow {
  description: string | null
  site_summary: string | null
  letter: StudioLetterView | null
  /** the latest version of each style — the drafts to pick from */
  variants: StudioLetterView[]
  letters: StudioLetterView[]
  request: { id: number; status: string; batch_id: string | null; error: string | null; kind: StudioLetterKind } | null
  vacancies: { id: number; name: string; status: string; score: number | null }[]
  sent_today?: number
  daily_limit?: number
  over_limit?: boolean
}

export interface StudioList {
  items: StudioRow[]
  total: number
  page: number
  per_page: number
  counts: { by_status: Record<string, number> } & Record<string, number | Record<string, number>>
  sent_today: number
  daily_limit: number
}

export interface StudioSettings {
  keywords: string[]
  only_with_vacancies: boolean
  signature: string
  offer_notes: string
}

export interface StudiosSummary {
  drafted: number
  replied: number
}

// ---- tenders of workspace.ru ----

// replied, declined: the organizer's answer, recorded by hand after responding
export type TenderStatus = 'new' | 'interesting' | 'drafting' | 'drafted' | 'responded' | 'replied' | 'declined' | 'skipped' | 'hidden'
export type TenderGroup = 'relevant' | 'open' | 'drafted' | 'responded' | 'closed' | 'all'

export interface TenderRow {
  id: number
  ws_id: number
  url: string
  title: string
  category: string | null
  section: string | null
  service: string | null
  budget: string | null
  budget_max: number | null
  published_on: string | null
  deadline_on: string | null
  state: 'open' | 'closed'
  open: boolean
  city: string | null
  organizer: string | null
  participants: number | null
  views: number | null
  fit_score: number | null
  fit_notes: string[]
  relevant: boolean
  status: TenderStatus
  status_label: string
  detail_error: string | null
  first_seen_at: string | null
  responded_at: string | null
  // when the organizer's answer (replied or declined) was recorded
  answered_at: string | null
  notes: string | null
}

export interface TenderReplyView {
  id: number
  version: number
  text: string
  origin: 'claude' | 'edited'
  batch_id: string | null
  needs_clarification: string[]
  warnings: string[]
  is_current: boolean
  created_at: string | null
}

export interface TenderDetail extends TenderRow {
  description: string | null
  conditions: { title: string; value: string }[]
  reply: TenderReplyView | null
  replies: TenderReplyView[]
  request: { id: number; status: string; batch_id: string | null; error: string | null } | null
}

export interface TenderList {
  items: TenderRow[]
  total: number
  page: number
  per_page: number
  counts: Record<TenderGroup, number>
}

export interface TenderSettings {
  sections: string[]
  min_score: number
  rates: string
  available_sections: { key: string; label: string }[]
}

export interface TendersSummary {
  new_relevant: number
}
