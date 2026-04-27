<!--
  HealthDonut — F6 账号池健康度环形图(SVG conic gradient)
  入参 segments: [{ key, value, color }]   color 用 rgba/hex 字面量
-->
<template>
  <div class="relative" :style="{ width: size + 'px', height: size + 'px' }">
    <svg :viewBox="`0 0 ${size} ${size}`" class="block">
      <!-- 背景圈 -->
      <circle :cx="size/2" :cy="size/2" :r="radius"
        fill="none" stroke="rgba(148, 163, 184, 0.10)" :stroke-width="thickness" />
      <!-- 段 -->
      <circle v-for="(seg, i) in arcs" :key="seg.key"
        :cx="size/2" :cy="size/2" :r="radius"
        fill="none" :stroke="seg.color" :stroke-width="thickness"
        :stroke-dasharray="`${seg.length} ${circumference - seg.length}`"
        :stroke-dashoffset="seg.offset"
        stroke-linecap="butt"
        :transform="`rotate(-90 ${size/2} ${size/2})`"
        class="transition-all duration-500" />
    </svg>
    <div class="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
      <div class="text-[10px] uppercase tracking-widest text-gray-500 leading-none">{{ centerLabel }}</div>
      <div class="text-2xl font-bold text-white tabular leading-none mt-1">{{ centerValue }}</div>
      <div v-if="centerHint" class="text-[10px] text-gray-500 mt-0.5">{{ centerHint }}</div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  size: { type: Number, default: 120 },
  thickness: { type: Number, default: 12 },
  segments: { type: Array, required: true }, // [{key,value,color}]
  centerLabel: { type: String, default: '总计' },
  centerValue: { type: [String, Number], default: 0 },
  centerHint: { type: String, default: '' },
})

const radius = computed(() => (props.size - props.thickness) / 2)
const circumference = computed(() => 2 * Math.PI * radius.value)
const total = computed(() => props.segments.reduce((s, x) => s + (x.value || 0), 0) || 1)

const arcs = computed(() => {
  let cumulative = 0
  return props.segments.map((seg) => {
    const len = (seg.value / total.value) * circumference.value
    const offset = -cumulative
    cumulative += len
    return { ...seg, length: len, offset }
  })
})
</script>
