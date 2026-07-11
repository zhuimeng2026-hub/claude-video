<template>
  <view class="page">
    <view class="header">
      <text class="task-id">{{ taskId }}</text>
      <text :class="['status', task.status]">{{ statusLabel(task.status) }}</text>
    </view>

    <view class="progress-section">
      <view class="progress-bar">
        <view class="progress-fill" :style="{ width: task.progress + '%' }"></view>
      </view>
      <text class="progress-text">{{ task.progress }}%</text>
    </view>

    <view v-if="task.message" class="message-card">
      <text class="message">{{ task.message }}</text>
    </view>

    <view v-if="task.status === 'completed' && result" class="result-card">
      <text class="result-title">处理结果</text>
      <view v-if="result.output" class="result-item">
        <text class="result-label">输出视频:</text>
        <text class="result-value">{{ result.output }}</text>
      </view>
      <view v-if="result.overlays" class="result-item">
        <text class="result-label">覆盖图:</text>
        <text class="result-value">{{ result.overlays?.length }} 张</text>
      </view>
      <view v-if="result.findings" class="result-item">
        <text class="result-label">发现品牌元素:</text>
        <text class="result-value">{{ result.findings?.length }} 个</text>
      </view>
      <button class="btn-download" @click="downloadResult">下载结果视频</button>
    </view>

    <view v-if="task.status === 'failed'" class="error-card">
      <text class="error-text">{{ task.error || '处理失败' }}</text>
    </view>
  </view>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const taskId = ref('')
const task = ref({ status: 'pending', progress: 0, message: '', error: '' })
const result = ref<any>(null)
let ws: WebSocket | null = null

const statusLabel = (s: string) => {
  const map: Record<string, string> = {
    pending: '等待中', decomposing: '分解中', analyzing: '分析中',
    modifying: '修改中', reassembling: '合成中', completed: '✅ 完成', failed: '❌ 失败',
  }
  return map[s] || s
}

const connectWS = () => {
  const host = location.hostname || 'localhost'
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${protocol}//${host}:8080/ws/workflow/${taskId.value}`)
  ws.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data)
      task.value.status = data.status || task.value.status
      task.value.progress = data.progress ?? task.value.progress
      task.value.message = data.message || ''
      if (data.status === 'completed') fetchResult()
    } catch (err) {}
  }
  ws.onerror = () => setTimeout(connectWS, 3000)
}

const fetchTask = async () => {
  try {
    const res: any = await new Promise((resolve, reject) => {
      uni.request({
        url: `/api/workflow/${taskId.value}`,
        success: (r) => resolve(r.data),
        fail: reject,
      })
    })
    task.value = { status: res.status, progress: res.progress, message: '', error: res.error || '' }
    if (res.status === 'completed') fetchResult()
  } catch (e) {}
}

const fetchResult = async () => {
  try {
    const res: any = await new Promise((resolve, reject) => {
      uni.request({
        url: `/api/workflow/${taskId.value}/result`,
        success: (r) => resolve(r.data),
        fail: reject,
      })
    })
    result.value = res
  } catch (e) {}
}

const downloadResult = () => {
  if (result.value?.output) {
    uni.downloadFile({
      url: `/api/workflow/${taskId.value}/download`,
      success: (res) => {
        uni.openDocument({ filePath: res.tempFilePath })
      },
    })
  }
}

onMounted(() => {
  const pages = getCurrentPages()
  const page = pages[pages.length - 1] as any
  taskId.value = page.$page?.options?.id || page.options?.id || ''
  if (taskId.value) {
    fetchTask()
    connectWS()
  }
})

onUnmounted(() => {
  if (ws) ws.close()
})
</script>

<style scoped>
.page { padding: 20rpx; }
.header { display: flex; justify-content: space-between; align-items: center; padding: 20rpx 0; }
.task-id { font-size: 28rpx; font-weight: 600; color: #333; }
.status { font-size: 24rpx; padding: 6rpx 16rpx; border-radius: 8rpx; }
.status.pending { background: #f0f0f0; color: #666; }
.status.completed { background: #e6f7e6; color: #2d8a2d; }
.status.failed { background: #fde6e6; color: #d32f2f; }
.status.analyzing, .status.modifying, .status.reassembling, .status.decomposing { background: #e6f0ff; color: #0066cc; }
.progress-section { display: flex; align-items: center; gap: 16rpx; margin: 20rpx 0; }
.progress-bar { flex: 1; height: 16rpx; background: #eee; border-radius: 8rpx; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #007aff, #5856d6); border-radius: 8rpx; transition: width 0.3s; }
.progress-text { font-size: 28rpx; font-weight: 600; color: #007aff; min-width: 80rpx; text-align: right; }
.message-card { background: #fff; border-radius: 12rpx; padding: 20rpx; margin-bottom: 20rpx; }
.message { font-size: 26rpx; color: #666; }
.result-card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-top: 20rpx; }
.result-title { font-size: 30rpx; font-weight: 600; display: block; margin-bottom: 16rpx; }
.result-item { display: flex; justify-content: space-between; padding: 12rpx 0; border-bottom: 1px solid #f0f0f0; }
.result-label { font-size: 26rpx; color: #666; }
.result-value { font-size: 26rpx; color: #333; }
.btn-download { background: #34c759; color: #fff; border: none; border-radius: 12rpx; padding: 20rpx; font-size: 28rpx; margin-top: 20rpx; }
.error-card { background: #fde6e6; border-radius: 12rpx; padding: 20rpx; margin-top: 20rpx; }
.error-text { color: #d32f2f; font-size: 26rpx; }
</style>
