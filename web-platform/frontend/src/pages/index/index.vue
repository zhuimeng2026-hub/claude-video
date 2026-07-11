<template>
  <view class="page">
    <view class="header">
      <text class="title">视频处理工作流</text>
      <text class="subtitle">分解 → 分析 → 修改 → 合成</text>
    </view>

    <view v-if="tasks.length === 0" class="empty">
      <text class="empty-text">暂无任务</text>
      <button class="btn-submit" @click="goSubmit">提交新任务</button>
    </view>

    <view v-else class="task-list">
      <view v-for="task in tasks" :key="task.id" class="task-card" @click="goTask(task.id)">
        <view class="task-header">
          <text class="task-id">{{ task.id }}</text>
          <text :class="['status-badge', task.status]">{{ statusLabel(task.status) }}</text>
        </view>
        <view class="progress-bar">
          <view class="progress-fill" :style="{ width: task.progress + '%' }"></view>
        </view>
        <view class="task-footer">
          <text class="task-time">{{ task.created_at }}</text>
          <text class="task-progress">{{ task.progress }}%</text>
        </view>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'

interface Task {
  id: string
  status: string
  progress: number
  created_at: string
}

const tasks = ref<Task[]>([])

const statusLabel = (s: string) => {
  const map: Record<string, string> = {
    pending: '等待中', decomposing: '分解中', analyzing: '分析中',
    modifying: '修改中', reassembling: '合成中', completed: '完成', failed: '失败',
  }
  return map[s] || s
}

const goSubmit = () => uni.switchTab({ url: '/pages/submit/submit' })
const goTask = (id: string) => uni.navigateTo({ url: `/pages/task/task?id=${id}` })

const fetchTasks = async () => {
  try {
    const res: any = await new Promise((resolve, reject) => {
      uni.request({
        url: '/api/workflow/tasks',
        success: (r) => resolve(r.data),
        fail: reject,
      })
    })
    if (Array.isArray(res)) tasks.value = res
  } catch (e) {
    console.error('Failed to fetch tasks', e)
  }
}

onMounted(fetchTasks)
</script>

<style scoped>
.page { padding: 20rpx; }
.header { text-align: center; padding: 40rpx 0; }
.title { font-size: 40rpx; font-weight: bold; display: block; }
.subtitle { font-size: 26rpx; color: #888; margin-top: 10rpx; display: block; }
.empty { text-align: center; padding: 100rpx 0; }
.empty-text { color: #999; font-size: 30rpx; display: block; margin-bottom: 30rpx; }
.btn-submit { background: #007aff; color: #fff; border: none; border-radius: 12rpx; padding: 20rpx 60rpx; font-size: 28rpx; }
.task-card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; box-shadow: 0 2rpx 8rpx rgba(0,0,0,0.05); }
.task-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16rpx; }
.task-id { font-size: 26rpx; color: #333; font-weight: 500; }
.status-badge { font-size: 22rpx; padding: 4rpx 16rpx; border-radius: 8rpx; }
.status-badge.pending { background: #f0f0f0; color: #666; }
.status-badge.completed { background: #e6f7e6; color: #2d8a2d; }
.status-badge.failed { background: #fde6e6; color: #d32f2f; }
.status-badge.analyzing, .status-badge.modifying, .status-badge.reassembling, .status-badge.decomposing {
  background: #e6f0ff; color: #0066cc; }
.progress-bar { height: 8rpx; background: #eee; border-radius: 4rpx; overflow: hidden; margin-bottom: 12rpx; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #007aff, #5856d6); border-radius: 4rpx; transition: width 0.3s; }
.task-footer { display: flex; justify-content: space-between; }
.task-time { font-size: 22rpx; color: #999; }
.task-progress { font-size: 24rpx; color: #007aff; font-weight: 500; }
</style>
