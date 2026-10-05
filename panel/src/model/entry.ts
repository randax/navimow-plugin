/** What is in a number field while it is being typed into, as the browser reports it. */
export interface Typed {
  text: string;
  /** Half a number, such as "-" or "1e": the browser gives its text as empty, and says so here. */
  unfinished: boolean;
}

/**
 * The number to save when the owner leaves a number field, as `{ number }`, where no number means
 * the value was taken out; or undefined when there is nothing to save. An emptied field takes the
 * value out. Half a number does not: left behind by a slip of the hand, it would otherwise look
 * like an emptied field and take out the value it was on its way to replacing.
 */
export function enteredNumber(typed: Typed | undefined, saved: number | undefined): { number?: number } | undefined {
  if (!typed || typed.unfinished) {
    return undefined;
  }
  const number = typed.text.trim() === '' ? undefined : Number(typed.text);
  return number === saved || Number.isNaN(number) ? undefined : { number };
}
