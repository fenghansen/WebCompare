import { english, serverErrors } from './messages-en.js';

export const languageKey = 'wc-language';

/** @typedef {'zh' | 'en'} Language */

/** @returns {Language} */
export function preferredLanguage() {
  return typeof window !== 'undefined' && localStorage.getItem(languageKey) === 'en' ? 'en' : 'zh';
}

let language = preferredLanguage();

export function currentLanguage() {
  return language;
}

/** @param {Language} next */
export function applyLanguage(next, persist = true) {
  language = next;
  document.documentElement.lang = next === 'en' ? 'en' : 'zh-CN';
  if (persist) localStorage.setItem(languageKey, next);
}

/** Translate interface text. A tagged template keeps interpolated data verbatim.
 * @param {string | TemplateStringsArray} source
 * @param {...unknown} values
 * @returns {string}
 */
export function t(source, ...values) {
  if (typeof source === 'string') {
    return language === 'en' && Object.hasOwn(english, source) ? english[source] : source;
  }
  const key = source.reduce((text, part, index) => text + (index ? '{' + (index - 1) + '}' : '') + part, '');
  const pattern = language === 'en' && Object.hasOwn(english, key) ? english[key] : key;
  return pattern.replace(/\{(\d+)\}/g, (_, index) => String(values[Number(index)]));
}

/** Localize application errors by their stable API code.
 * @param {unknown} detail
 * @param {string} fallback
 */
export function serverMessage(detail, fallback) {
  const record = detail && typeof detail === 'object' ? /** @type {{message?: unknown, code?: unknown, path?: unknown}} */ (detail) : null;
  const message = typeof detail === 'string' ? detail : record?.message;
  if (typeof message !== 'string' || !message) return fallback;
  if (language === 'zh' || !/\p{Script=Han}/u.test(message)) return message;
  const code = record?.code;
  const localized = typeof code === 'string' && Object.hasOwn(serverErrors, code) ? serverErrors[code] : fallback;
  return localized + (typeof record?.path === 'string' ? ' · ' + record.path : '');
}

/** Translate the diagnostic wording around RAW sidecar names and values.
 * @param {string} message
 * @returns {string}
 */
export function rawMessage(message) {
  if (language === 'zh') return message;
  return t(message)
    .replace(/^(.+) 的 (\S+) 不合法，已忽略该自动参数。$/, '$1 has an invalid $2 value. The value was skipped.')
    .replace(/^已忽略 (.+)：未声明 schema=webcompare.raw.v1，未将其作为 RAW 参数。$/, 'Ignored $1 because it does not declare schema=webcompare.raw.v1.')
    .replace(/^(.+) 的黑位来源为工程占位或未知，未作为真实 BL 使用。$/, 'Skipped the placeholder or unknown black level from $1.')
    .replace(/^(.+) 的 (\S+) 为占位或未知，已忽略。$/, 'Skipped the placeholder or unknown $2 from $1.')
    .replace(/^未提供 RAW 白位，自动 white_level 使用 (\d+) bit 满量程 (.+) DN。$/, 'RAW white level is missing. Using the $1-bit full scale of $2 DN.')
    .replace(/^未使用四相 BL：(.+)$/, (_, origin) => 'No four-phase black level · ' + t(origin))
    .replace(/^(.+)（四相均值，仅用于表示）$/, (_, origin) => t(origin) + ' (four-phase mean, display only)');
}
