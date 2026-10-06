const LIST = new Intl.ListFormat('en-GB', { type: 'conjunction' });

/** Names as a message gives them: each in quotes, as a list: "a", "b" and "c". */
export const quoted = (names: string[]): string => LIST.format(names.map((name) => `"${name}"`));
