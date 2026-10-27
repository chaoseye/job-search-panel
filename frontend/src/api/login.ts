/** The sign-in page of the public address (a server page outside the SPA): after «Выйти» (`out`) or when the session
 *  has ended (`expired`; back to this page after the next sign-in). */
export function goToLogin(reason: 'out' | 'expired'): void {
  const next = encodeURIComponent(window.location.pathname + window.location.search)
  window.location.assign(reason === 'out' ? '/login?out=1' : `/login?expired=1&next=${next}`)
}
