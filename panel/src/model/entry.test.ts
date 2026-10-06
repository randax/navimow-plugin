import { enteredNumber } from './entry';

const typed = (text: string, unfinished = false) => ({ text, unfinished });

describe('enteredNumber', () => {
  test('a number typed in place of another is the one to save', () => {
    expect(enteredNumber(typed('59.965'), 59.964)).toEqual({ number: 59.965 });
    expect(enteredNumber(typed(' -20 '), 20)).toEqual({ number: -20 });
    expect(enteredNumber(typed('0'), undefined)).toEqual({ number: 0 });
  });

  test('a field emptied on purpose takes the value out', () => {
    expect(enteredNumber(typed(''), 20)).toEqual({ number: undefined });
  });

  // A browser reports a number field holding only "-" or "1e" as empty, and as unfinished.
  test('half a number, left behind on the way to typing one, changes nothing', () => {
    expect(enteredNumber(typed('', true), 20)).toBeUndefined();
  });

  test('nothing typed, or the value already saved typed again, changes nothing', () => {
    expect(enteredNumber(undefined, 20)).toBeUndefined();
    expect(enteredNumber(typed('20.0'), 20)).toBeUndefined();
    expect(enteredNumber(typed(''), undefined)).toBeUndefined();
  });

  test('text that is not a number changes nothing', () => {
    expect(enteredNumber(typed('north'), 20)).toBeUndefined();
  });
});
