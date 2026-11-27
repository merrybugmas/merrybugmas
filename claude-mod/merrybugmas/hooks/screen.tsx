import type { ClientModule, JsonValue } from 'claude-code'

/**
 * 패널 맨 아래 한 줄.
 *  - 잡은 쥐 목록: 이슈 ID 를 클릭하면 그 쥐의 처리 기록(잡은 쥐 기록 창)이 열린다. 🏆 을 누르면 전체 기록.
 *  - 이 줄을 클릭하면 키보드(방향키·Enter·Tab·⌫)도 게임으로 넘어간다.
 * 화면 자체는 hooks 모듈이 Raster 로 그린다 (Client 트리는 크기 한도가 작아서).
 */
type Props = { caught?: string[]; hist?: boolean }
type Ev = { id: number; key?: string; open?: string; toggle?: true }
type Hit = { x0: number; x1: number; open?: string; toggle?: true }
type State = { n: number; events: Ev[]; on: boolean; hits: Hit[] }

const isWide = (c: number) => (c >= 0x1100 && c <= 0x115f) || (c >= 0x2e80 && c <= 0xa4cf) || (c >= 0xac00 && c <= 0xd7a3)
  || (c >= 0xf900 && c <= 0xfaff) || (c >= 0xff00 && c <= 0xff60) || c >= 0x1f300
const cw = (s: string) => [...s].reduce((a, ch) => a + (isWide(ch.codePointAt(0) ?? 0) ? 2 : 1), 0)

const KeyBar: ClientModule<JsonValue, State> = (raw, s) => {
  const { Box, Text } = s.elements
  const props = (raw ?? {}) as Props
  if (s.state === undefined) {
    // 한 객체를 계속 쓴다: 리스너가 읽는 hits 를 그릴 때마다 여기에 갱신한다
    const st: State = { n: 0, events: [], on: false, hits: [] }
    const push = (ev: Omit<Ev, 'id'>) => {
      st.n += 1
      st.events = [...st.events, { id: st.n, ...ev }].slice(-12)
      s.post({ events: st.events } as unknown as JsonValue)
    }
    s.onKey(k => push({ key: k.key }))
    s.onPointer(p => {
      if (p.type !== 'down') return
      const hit = st.hits.find(h => p.x >= h.x0 && p.x < h.x1)
      if (hit?.open) push({ open: hit.open })
      else if (hit?.toggle) push({ toggle: true })
      if (!st.on) { st.on = true; s.setState(st) }
    })
    s.setState(st)
  }
  const st = s.state
  const on = st?.on ?? false
  const caught = props.caught ?? []
  // 조각과 클릭 영역을 같이 만든다 (폭: 한글·이모지 두 칸)
  const parts: { text: string; color: string; bg?: string; bold?: boolean; hit?: { open?: string; toggle?: true } }[] = [
    { text: props.hist ? ' 🏆 기록 닫기 ' : ' 🏆 잡은 쥐 ', color: '#ffd75f', bg: '#5f0000', bold: true, hit: { toggle: true } },
    { text: ' ', color: '#8a8a8a' },
  ]
  if (caught.length === 0) parts.push({ text: '아직 없음 ', color: '#8a8a8a' })
  for (const id of caught) {
    parts.push({ text: ` ${id} `, color: '#ffffd7', bg: '#3a3a3a', hit: { open: id } })
    parts.push({ text: ' ', color: '#8a8a8a' })
  }
  parts.push({
    text: on ? '│ ⌨ 키보드 연결됨 (↑↓←→ Enter · ⌫ 뒤로 · Esc 입력창)' : '│ ⌨ 게임 화면을 한 번 클릭하면 방향키·Enter 로 조작 · Esc 입력창',
    color: on ? '#ffd75f' : '#8a8a8a',
  })
  const hits: Hit[] = []
  let x = 0
  for (const p of parts) {
    const w = cw(p.text)
    if (p.hit) hits.push({ x0: x, x1: x + w, ...p.hit })
    x += w
  }
  if (st) st.hits = hits
  return (
    <Box>
      <Text wrap="truncate-end">
        {parts.map((p, i) => <Text key={`p${i}`} color={p.color} backgroundColor={p.bg} bold={p.bold}>{p.text}</Text>)}
      </Text>
    </Box>
  )
}

export default KeyBar
