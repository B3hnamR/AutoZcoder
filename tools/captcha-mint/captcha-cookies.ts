import CookieStringUtility from "happy-dom/lib/cookie/urilities/CookieStringUtility.js";
import type CookieContainer from "happy-dom/lib/cookie/CookieContainer.js";

/** Use the installed Happy DOM parser: key, originURL and Date expiry matter. */
export function storeCookieStrings(container: CookieContainer, url: string, values: string[]): void {
  const originURL = new URL(url);
  for (const raw of values) {
    const cookie = CookieStringUtility.stringToCookie(originURL, raw);
    // 20.14.6 compares expiry with '<', so Max-Age=0 can survive this tick.
    if (cookie?.expires && cookie.expires.getTime() <= Date.now()) cookie.expires = new Date(0);
    if (cookie) container.addCookies([cookie]);
  }
}

export function serializeCookies(container: CookieContainer, url: string): string {
  return CookieStringUtility.cookiesToString(container.getCookies(new URL(url), false));
}
