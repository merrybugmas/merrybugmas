import type { Register } from 'claude-code'

/**
 * MERRY BUGMAS 패널 — 프로젝트의 xmas_market.py 를 `--stream` 으로 띄우고,
 * 한 줄씩 오는 프레임(JSON)을 옆 패널에 Raster 로 그린다. 모델을 부르지 않는다.
 * 게임 화면 위에 투명 입력판(pad.tsx)을 덮어, 한 번 클릭하면 방향키·Enter·클릭이 게임으로 간다.
 *
 *   /merrybugmas            패널 열기 (게임을 타이틀부터 새로 시작, 실연동 --live: .env 에 설정된 Sentry·트래픽만 붙고 나머지는 mock)
 *   /merrybugmas mock       mock 모드: Sentry·트래픽 둘 다 mock 데이터 (.env 의 실연동 설정을 무시한다)
 *   /merrybugmas ... noai   OpenAI 를 부르지 않는다 (키워드 판정·규칙 엘프). mock 이나 live 뒤에 붙인다
 *   /merrybugmas stop       게임 끄고 패널 닫기
 *
 * 패널 → 게임 입력은 <프로젝트>/.runtime/pane_cmd ('번호<TAB>명령' 줄들, 번호가 커진 것만 읽힌다).
 * 채팅의 ./market 명령(.runtime/cmd)도 그대로 먹는다.
 */

const PANE = 'merrybugmas'
const TITLE = '🎄 MERRY BUGMAS'
const SCRIPT = 'xmas_market.py'

type Block = { cells: string; rows: number } | { runs: [string, string, string, number][] }
/** 병정이 연 실제 PR: [번호, URL, 이슈 ID, 제목] */
type Pr = [number, string, string, string]
type Frame = { cols: number; blocks: Block[]; note: string | null; caught?: string[]; hist?: boolean; prs?: Pr[] }

let latest: Frame = { cols: 0, blocks: [], note: null }
let child: { id: number; stop: () => void } | null = null
/** start() 가 진행 중인지: await 사이에 두 번 불려 게임이 둘 뜨지 않게 (session.start 와 첫 그리기가 겹친다) */
let starting = false
let childSeq = 0
let projectDir: string | null = null
/** 실행 모드: live = --live (실연동), mock = 둘 다 mock. noai = OpenAI 끔. 다음 /merrybugmas 까지 유지 */
let mode: { live: boolean; noai: boolean } = { live: true, noai: false }
let size = { cols: 0, rows: 0 }
const seenEvent: Record<string, number> = { keys: 0, pad: 0 }
let cmdSeq = 0
let cmdLines: string[] = []
let writing: Promise<void> = Promise.resolve()
/** 지금 패널에 그려진 프레임의 모양 (Raster 칸 수 + 글자 줄 내용). 같으면 다시 그리지 않고 Raster 만 blit 한다 */
let drawnShape = ''

function shapeOf(f: Frame): string {
  return `${f.cols}|${f.note ?? ''}|${f.hist ? 1 : 0}|${(f.caught ?? []).join(',')}|${(f.prs ?? []).map(p => p[0]).join(',')}|` +
    f.blocks.map(b => ('cells' in b ? `R${b.rows}` : JSON.stringify(b.runs))).join('|')
}

/** 새 프레임 보여 주기: 모양이 그대로면 Raster 칸만 바꿔 칠하고(빠름), 아니면 패널을 다시 그린다 */
function present($: any) {
  if (shapeOf(latest) !== drawnShape) {
    $.ui.invalidate('ui.render')
    return
  }
  latest.blocks.forEach((b, i) => {
    if (!('cells' in b)) return
    void $.ui.blit({ requestId: PANE, key: `r${i}`, cells: b.cells, columns: latest.cols, rows: b.rows })
      .then((r: { deny?: string }) => { if (r?.deny) { drawnShape = ''; $.ui.invalidate('ui.render') } })
      .catch(() => { drawnShape = ''; $.ui.invalidate('ui.render') })
  })
}

async function findProject($: any): Promise<string | null> {
  if (projectDir) return projectDir
  const env: Record<string, string> = await $.process.hookEnv().catch(() => ({}))
  const dir = (env.CLAUDE_PROJECT_DIR ?? '').replace(/\/+$/, '')
  if (dir && (await $.fs.exists(`${dir}/${SCRIPT}`).catch(() => false))) projectDir = dir
  return projectDir
}

async function childEnv($: any): Promise<Record<string, string>> {
  const home: string | undefined = await $.env.get('HOME')
  const path: string | undefined = await $.env.get('PATH')
  const dirs = [...(home ? [`${home}/.local/bin`] : []), '/opt/homebrew/bin', '/usr/local/bin', path || '/usr/bin:/bin']
  return { PATH: dirs.join(':'), PYTHONUNBUFFERED: '1', ...(home ? { HOME: home } : {}) }
}

/** 게임에 명령 한 줄 보내기: 최근 40줄을 번호와 함께 파일에 통째로 쓴다 */
function send($: any, cmd: string) {
  if (!projectDir) return
  const dir = projectDir
  cmdSeq = Math.max(cmdSeq + 1, Date.now() * 10)
  cmdLines = [...cmdLines, `${cmdSeq}\t${cmd}`].slice(-40)
  const text = cmdLines.join('\n') + '\n'
  writing = writing.then(() => $.fs.write(`${dir}/.runtime/pane_cmd`, text)).catch(() => undefined)
}

async function start($: any) {
  if (child || starting) return
  starting = true
  try {
    await startChild($)
  } finally {
    starting = false
  }
}

async function startChild($: any) {
  const dir = await findProject($)
  if (!dir) {
    latest = { cols: 0, blocks: [], note: `이 프로젝트에 ${SCRIPT} 가 없습니다. merrybugmas 폴더에서 Claude Code 를 열어 주세요.` }
    return
  }
  const id = ++childSeq
  let stopped = false
  const cols = size.cols || 100
  const rows = Math.max(22, size.rows || 30)
  latest = { cols: 0, blocks: [], note: 'MERRY BUGMAS 시작 중…' }
  const stream = $.process.spawn({
    argv: ['/usr/bin/env', 'python3', `${dir}/${SCRIPT}`, '--stream', ...(mode.live ? ['--live'] : []),
      ...(mode.noai ? ['--no-ai'] : []), '--size', `${cols}x${rows}`],
    cwd: dir,
    env: await childEnv($),
  })
  child = { id, stop: () => { stopped = true; void stream.return?.(undefined) } }
  void (async () => {
    let buf = ''
    let err = ''
    try {
      for await (const piece of stream) {
        if (stopped) break
        if (piece.stream === 'stderr') { err = (err + piece.text).slice(-600); continue }
        if (piece.stream !== 'stdout' || !piece.text) continue
        buf += piece.text
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        const last = lines.filter(l => l.trim()).pop()
        if (!last) continue
        try {
          const o = JSON.parse(last)
          latest = { cols: o.cols ?? 0, blocks: o.blocks ?? [], note: null, caught: o.caught ?? [], hist: !!o.hist, prs: o.prs ?? [] }
          present($)
        } catch { /* 잘린 줄 */ }
      }
    } catch (e) {
      err = String((e as Error)?.message ?? e)
    }
    if (child?.id === id) {
      child = null
      if (!stopped) latest = { cols: 0, blocks: [], note: `게임이 멈췄습니다. /merrybugmas 로 다시 시작${err ? ` · ${err.trim().split('\n').pop()}` : ''}` }
      $.ui.invalidate('ui.render')
    }
  })()
}

function stop() {
  child?.stop()
  child = null
}

let commandReady = false
/** /merrybugmas 등록. 실패하면 2초 뒤 다시 (tries 번까지) */
async function registerCommand($: any, tries: number): Promise<void> {
  if (commandReady) return
  try {
    await $.command.register({
      name: 'merrybugmas',
      description: 'MERRY BUGMAS 대시보드를 옆 패널에 띄운다 (mock = 둘 다 mock 데이터, noai = OpenAI 끔, stop = 끄기)',
      argumentHint: '[mock|live] [noai] | stop',
      aliases: ['xmas', 'marrybugmas'],
      immediate: true,
    })
    commandReady = true
  } catch {
    if (tries > 1) $.clock.after(2000, () => registerCommand($, tries - 1))
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // 리로드 직후엔 세션이 아직 안 붙어 등록이 거부될 수 있다: 2초마다 다시 시도
    void registerCommand($, 15)
    const started = await next(e)
    // merrybugmas 프로젝트에서 연 세션이면 바로 띄운다
    if (await findProject($)) {
      void start($)
      void $.ui.open({ id: PANE, title: TITLE })
    }
    return started
  })

  on('command.run', { command: 'merrybugmas' }, async ($, e) => {
    if (/^(stop|off|끄기)$/i.test(e.args.trim())) {
      stop()
      await $.ui.close({ id: PANE })
      return { text: 'MERRY BUGMAS 를 끄고 패널을 닫았습니다.' }
    }
    // 열 때마다 타이틀부터 새로 시작한다. 인자: mock/live (기본 live), noai
    const words = e.args.trim().toLowerCase().split(/\s+/).filter(Boolean)
    mode = { live: !words.some(w => /^(mock|목|모크)$/.test(w)), noai: words.some(w => /^(noai|no-ai)$/.test(w)) }
    stop()
    latest = { cols: 0, blocks: [], note: null }
    await start($)
    await $.ui.open({ id: PANE, title: TITLE, focus: true })
    const how = `${mode.live ? '실연동(--live: .env 에 설정된 Sentry·트래픽)' : 'mock 모드(Sentry·트래픽 모두 mock)'}${mode.noai ? ' · OpenAI 끔' : ''}`
    return { text: projectDir ? `MERRY BUGMAS 를 타이틀부터 새로 시작했습니다 - ${how}. 게임 화면을 한 번 클릭하면 방향키·Enter 로 조작합니다 (Esc 는 입력창으로).` : latest.note ?? '' }
  })

  // 메시지를 보낼 때도 등록을 확인한다 (세션이 붙은 뒤라 확실히 된다)
  on('prompt.submit', async ($, e, next) => {
    await registerCommand($, 1)
    return next(e)
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) stop()
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    stop()
    return next(e)
  })

  // 맨 아래 한 줄 Client(screen.tsx)가 누른 키를 보낸다
  on('ui.message', async ($, e, next) => {
    const el = e.element
    if (el !== 'keys' && el !== 'pad') return next(e)
    type Ev = { id: number; key?: string; open?: string; toggle?: true; click?: [number, number] }
    const evs = ((e.data ?? {}) as { events?: Ev[] }).events ?? []
    const top = Math.max(0, ...evs.map(ev => ev.id))
    if (top < (seenEvent[el] ?? 0)) seenEvent[el] = 0 // 패널을 새로 열면 Client 번호가 1부터 다시 센다
    for (const ev of evs) {
      if (ev.id <= (seenEvent[el] ?? 0)) continue
      seenEvent[el] = ev.id
      if (ev.open) send($, `history ${ev.open}`) // 잡은 쥐를 누르면 그 쥐의 처리 기록
      else if (ev.toggle) send($, 'history')
      else if (ev.click) send($, `click ${ev.click[0]} ${ev.click[1]}`) // 게임 화면 클릭
      else if (ev.key) send($, `key ${ev.key}`)
    }
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e) as any
    const { Box, Text, Button } = els
    if (!els.Raster) return <Box><Text dimColor>이 화면에서는 MERRY BUGMAS 를 그릴 수 없습니다 (터미널 전용).</Text></Box>
    const { Raster, Client, Link } = els
    // 게임 화면 크기 = 패널 본문 - 버튼 줄(좁으면 두 줄) - 키보드 줄
    const cols = Math.max(20, e.props.bodyColumns ?? 100)
    const rows = Math.max(22, (e.props.scroll?.bodyRows ?? 32) - 3)
    if (cols !== size.cols || rows !== size.rows) {
      size = { cols, rows }
      send($, `size ${cols}x${rows}`)
    }
    if (!child && !latest.note) void start($)
    const btn = (key: string, hotkey: string, label: string, cmd: string) => (
      <Button key={`k-${key}`} plain hotkey={hotkey} onPress={() => send($, cmd)}>
        <Text backgroundColor="#3a3a3a" color="#ffffd7"> {label} </Text>
      </Button>
    )
    const fits = latest.cols > 0 && latest.cols <= cols
    drawnShape = shapeOf(latest)
    const gameRows = latest.blocks.reduce((n, b) => n + ('cells' in b ? b.rows : 1), 0)
    return (
      <Box flexDirection="column">
        {!fits && <Text dimColor>{latest.note ?? 'MERRY BUGMAS 시작 중…'}</Text>}
        {fits && (
          <Box flexDirection="column" position="relative">
            {latest.blocks.map((b, i) => ('cells' in b
              ? <Raster key={`r${i}`} columns={latest.cols} rows={b.rows} cells={b.cells} />
              : (
                <Text key={`t${i}`} wrap="truncate-end">
                  {b.runs.map((r, j) => <Text key={`t${i}-${j}`} color={r[1]} backgroundColor={r[2]} bold={r[3] === 1}>{r[0]}</Text>)}
                </Text>
              )))}
            {/* 투명 입력판: 게임 화면을 클릭하면 방향키·Enter 가 게임으로 간다 */}
            <Box position="absolute" top={0} left={0}>
              <Client key="pad" module="./pad.tsx" width={latest.cols} height={Math.max(1, gameRows)} />
            </Box>
          </Box>
        )}
        <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
          {btn('menu', 'm', 'm 메뉴', 'key m')}
          {btn('ov', '0', '0 개요', 'key 0')}
          {btn('tr', '1', '1 트래픽', 'key 1')}
          {btn('se', '2', '2 Sentry', 'key 2')}
          {btn('spike', 't', 't 급증', 'key t')}
          {btn('bugs', 'b', 'b 쥐떼', 'key b')}
          {btn('hist', 'r', 'r 🏆기록', 'key r')}
        </Box>
        {(latest.prs ?? []).length > 0 && (
          <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
            <Text bold color="#5fd75f">🔀 병정이 연 PR (클릭하면 열림)</Text>
            {(latest.prs ?? []).map(p => (
              <Text key={`pr-${p[0]}`} wrap="truncate-end">
                <Link href={p[1]}>PR #{p[0]}</Link>
                <Text dimColor> {p[2]}</Text>
              </Text>
            ))}
          </Box>
        )}
        <Client key="keys" module="./screen.tsx" props={{ caught: latest.caught ?? [], hist: !!latest.hist }} width="100%" height={1} />
      </Box>
    )
  })
}
