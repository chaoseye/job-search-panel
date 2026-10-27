/** Joins the truthy class names. */
export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

/** True when the classes have a utility of this kind ('w-' → w-16, sm:w-72; 'min-h-' → min-h-40). Two utilities of one
 *  kind on an element do not override each other by their order in the class list, only by their order in the
 *  stylesheet — so a component's default steps aside when the caller sets its own. */
export function hasClass(className: string | null | undefined, prefix: string): boolean {
  return Boolean(className && new RegExp(`(^|\\s)(?:[a-z0-9-]+:)*${prefix.replace(/[-[\]]/g, '\\$&')}`).test(className))
}

export const hasWidth = (className: string | null | undefined) => hasClass(className, 'w-')
