<template>
  <section aria-label="调整排期">
    <label>改期到（北京时间）
      <input v-model="input" type="datetime-local" step="1" :disabled="saving" @input="reset">
    </label>
    <div class="buttons">
      <el-button :disabled="!input || saving" :loading="checking" @click="preview">查看可用时间</el-button>
      <el-button :disabled="!input || checking" :loading="saving" @click="save">{{ proposal && proposal.available !== proposal.requested ? '按建议时间保存' : '保存改期' }}</el-button>
    </div>
    <p v-if="proposal" role="status">{{ proposal.available === proposal.requested ? '所选时间可用' : '按账号及平台规则可安排至' }}　{{ date(proposal.available) }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>

<script setup>
import { ref } from 'vue'

const props = defineProps({ job: Object, api: Function, date: Function })
const emit = defineEmits(['saved'])
const input = ref('')
const proposal = ref(null)
const checking = ref(false)
const saving = ref(false)
const error = ref('')
let sequence = 0

function reset() {
  sequence++
  checking.value = false
  proposal.value = null
  error.value = ''
}

function requested() {
  const stamp = Date.parse(input.value + '+08:00') / 1000
  if (!Number.isInteger(stamp)) throw Error('请选择北京时间，精确到分钟或秒。')
  return stamp
}

async function preview() {
  reset()
  const current = sequence
  checking.value = true
  try {
    const answer = await props.api('jobs/' + props.job.id + '/available-slot?time=' + requested())
    if (sequence !== current) return
    if (answer.expected_slot !== props.job.slot || answer.expected_execution !== Math.max(props.job.slot, props.job.due)) {
      throw Error('任务排期已变化，请重新打开任务详情。')
    }
    proposal.value = answer
  } catch (err) {
    if (sequence === current) error.value = err.message
  } finally {
    if (sequence === current) checking.value = false
  }
}

async function save() {
  const job = props.job
  saving.value = true
  error.value = ''
  try {
    const when = proposal.value?.available ?? requested()
    if (when === job.slot) throw Error('此时间已是当前计划时间。')
    const saved = await props.api('jobs/' + job.id + '/reschedule', {
      time: when, expected_available: when, expected_slot: job.slot, expected_execution: Math.max(job.slot, job.due)
    })
    emit('saved', job.id, saved.slot)
  } catch (err) {
    proposal.value = null
    error.value = err.message
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
section { margin: 12px 0 }
.buttons { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px }
.buttons :deep(.el-button) { margin: 0 }
[role="alert"] { color: var(--el-color-danger) }
</style>
