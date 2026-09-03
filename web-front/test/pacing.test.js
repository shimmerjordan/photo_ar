import { test } from 'node:test'
import assert from 'node:assert/strict'
import { PACE, MOTION_STILL, IDLE_SLOW_MS, IDLE_MS, sendInterval } from '../public/pacing.js'

test('常量数值钉住，改动必须显式改这条测试', () => {
  assert.deepEqual(PACE, { FAST: 16, STILL: 100, SLOW: 300, IDLE: 700 })
  assert.equal(MOTION_STILL, 0.08)
  assert.equal(IDLE_SLOW_MS, 10_000)
  assert.equal(IDLE_MS, 30_000)
})

test('锁定且静止 → STILL；锁定且在动 → FAST', () => {
  assert.equal(sendInterval({ locked: true, motion: 0, idleMs: 0, reason: 'ok' }), PACE.STILL)
  assert.equal(sendInterval({ locked: true, motion: MOTION_STILL - 0.01, idleMs: 0, reason: 'ok' }), PACE.STILL)
  assert.equal(sendInterval({ locked: true, motion: MOTION_STILL, idleMs: 0, reason: 'ok' }), PACE.FAST)
  assert.equal(sendInterval({ locked: true, motion: 1, idleMs: 0, reason: 'ok' }), PACE.FAST)
})

test('扫描：10 秒内满速，10~30 秒 SLOW，超 30 秒 IDLE', () => {
  assert.equal(sendInterval({ locked: false, motion: 0, idleMs: 0, reason: 'weak' }), PACE.FAST)
  assert.equal(sendInterval({ locked: false, motion: 0, idleMs: IDLE_SLOW_MS - 1, reason: 'weak' }), PACE.FAST)
  assert.equal(sendInterval({ locked: false, motion: 0, idleMs: IDLE_SLOW_MS, reason: 'weak' }), PACE.SLOW)
  assert.equal(sendInterval({ locked: false, motion: 0, idleMs: IDLE_MS, reason: 'weak' }), PACE.IDLE)
})

test('对着白墙（no_features）直接 IDLE，不等 30 秒', () => {
  assert.equal(sendInterval({ locked: false, motion: 0, idleMs: 0, reason: 'no_features' }), PACE.IDLE)
})

test('锁定期间 idleMs 与 reason 不起作用', () => {
  assert.equal(sendInterval({ locked: true, motion: 1, idleMs: IDLE_MS * 2, reason: 'no_features' }), PACE.FAST)
})

test('缺参数按最保守（满速）', () => {
  assert.equal(sendInterval({}), PACE.FAST)
  assert.equal(sendInterval({ locked: true }), PACE.FAST)   // motion 未知 → 当作在动
})
