/** Hash routing keeps the current app session; identity is checked by App on entry. */
export function openChat() {
  location.hash = '#/chat'
}
