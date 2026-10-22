export const GEN = 10n ** 18n;
export const MINUTE = 60;
export const HOUR = 3600;
export const DAY = 86400;

export const sleepMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export const lc = (value) => String(value ?? '').toLowerCase();
export const sameAddress = (a, b) => lc(a) === lc(b);

export function fmtWei(wei) {
  let value = BigInt(wei);
  const negative = value < 0n;
  if (negative) value = -value;
  const whole = value / GEN;
  const fraction = (value % GEN).toString().padStart(18, '0').replace(/0+$/, '');
  return (negative ? '-' : '') + whole + (fraction ? '.' + fraction.slice(0, 6) : '') + ' GEN';
}

export function parseJson(value) {
  if (typeof value === 'string') {
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }
  return value;
}

export const shortHash = (hash) => (hash ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : '');

export function errorText(error) {
  if (!error) return '';
  return String(error.shortMessage || error.message || error).replace(/\s+/g, ' ').slice(0, 300);
}

/** Integer-valued JS numbers become bigint so the calldata encoder never sees a float. */
export function normaliseArgs(args) {
  return args.map((a) => (typeof a === 'number' && Number.isInteger(a) ? BigInt(a) : a));
}

/** The share of a price a party gets, exactly as DealEscrow computes it. */
export function expectedSplit(price, buyerPct, feeBps) {
  const p = BigInt(price);
  const buyer = (p * BigInt(buyerPct)) / 100n;
  const gross = p - buyer;
  const fee = (gross * BigInt(feeBps)) / 10000n;
  return { buyer, seller: gross - fee, fee };
}
