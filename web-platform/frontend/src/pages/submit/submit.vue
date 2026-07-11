<template>
  <view class="page">
    <view class="section">
      <text class="section-title">视频来源</text>
      <input v-model="form.video_url" class="input" placeholder="输入视频 URL 或本地路径" />
      <button class="btn-upload" @click="chooseVideo">选择本地视频</button>
    </view>

    <view class="section">
      <text class="section-title">替换配置</text>
      <view v-for="(item, idx) in form.replacements" :key="idx" class="replace-card">
        <view class="card-header">
          <text class="card-title">替换项 {{ idx + 1 }}</text>
          <text class="btn-remove" @click="removeItem(idx)">✕</text>
        </view>
        <input v-model="item.label" class="input-sm" placeholder="标签 (如: BYD徽标)" />
        <view class="row">
          <input v-model.number="item.time_start" class="input-half" type="digit" placeholder="开始秒" />
          <input v-model.number="item.time_end" class="input-half" type="digit" placeholder="结束秒" />
        </view>
        <view class="row">
          <input v-model.number="item.bbox_x" class="input-quad" type="digit" placeholder="X%" />
          <input v-model.number="item.bbox_y" class="input-quad" type="digit" placeholder="Y%" />
          <input v-model.number="item.bbox_w" class="input-quad" type="digit" placeholder="W%" />
          <input v-model.number="item.bbox_h" class="input-quad" type="digit" placeholder="H%" />
        </view>
        <input v-model="item.replace_text" class="input-sm" placeholder="替换文字 (如: AURORA X)" />
      </view>
      <button class="btn-add" @click="addItem">+ 添加替换项</button>
    </view>

    <view class="section">
      <text class="section-title">处理模式</text>
      <picker :range="detailOptions" @change="onDetailChange">
        <view class="picker">{{ form.detail }}</view>
      </picker>
    </view>

    <button class="btn-submit" :disabled="submitting" @click="submit">
      {{ submitting ? '提交中...' : '提交任务' }}
    </button>
  </view>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'

interface ReplaceItem {
  label: string
  time_start: number | null
  time_end: number | null
  bbox_x: number | null
  bbox_y: number | null
  bbox_w: number | null
  bbox_h: number | null
  replace_text: string
}

const detailOptions = ['efficient', 'balanced', 'token-burner']
const submitting = ref(false)

const form = reactive({
  video_url: '',
  video_file: '',
  detail: 'balanced',
  replacements: [
    { label: '', time_start: null, time_end: null, bbox_x: null, bbox_y: null, bbox_w: null, bbox_h: null, replace_text: 'AURORA X' } as ReplaceItem,
  ] as ReplaceItem[],
})

const addItem = () => {
  form.replacements.push({ label: '', time_start: null, time_end: null, bbox_x: null, bbox_y: null, bbox_w: null, bbox_h: null, replace_text: 'AURORA X' })
}
const removeItem = (idx: number) => {
  form.replacements.splice(idx, 1)
}
const onDetailChange = (e: any) => {
  form.detail = detailOptions[e.detail.value]
}
const chooseVideo = () => {
  uni.chooseVideo({
    success: (res) => { form.video_file = res.tempFilePath },
  })
}

const submit = async () => {
  if (!form.video_url && !form.video_file) {
    uni.showToast({ title: '请输入视频来源', icon: 'none' })
    return
  }
  submitting.value = true
  try {
    const payload = {
      video_url: form.video_url,
      video_file: form.video_file,
      detail: form.detail,
      replacements: form.replacements
        .filter(r => r.label && r.time_start != null && r.time_end != null)
        .map(r => ({
          label: r.label,
          time_window: [r.time_start, r.time_end],
          bbox_pct: { x: r.bbox_x || 0, y: r.bbox_y || 0, w: r.bbox_w || 20, h: r.bbox_h || 10 },
          replace_text: r.replace_text,
        })),
    }
    const res: any = await new Promise((resolve, reject) => {
      uni.request({
        url: '/api/workflow/submit',
        method: 'POST',
        data: payload,
        success: (r) => resolve(r.data),
        fail: reject,
      })
    })
    if (res.task_id) {
      uni.showToast({ title: '已提交', icon: 'success' })
      uni.navigateTo({ url: `/pages/task/task?id=${res.task_id}` })
    }
  } catch (e) {
    uni.showToast({ title: '提交失败', icon: 'none' })
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.page { padding: 20rpx; }
.section { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.section-title { font-size: 30rpx; font-weight: 600; display: block; margin-bottom: 16rpx; }
.input { border: 1px solid #ddd; border-radius: 10rpx; padding: 16rpx; font-size: 28rpx; width: 100%; box-sizing: border-box; margin-bottom: 12rpx; }
.btn-upload { background: #f0f0f0; color: #333; border: 1px dashed #ccc; border-radius: 10rpx; padding: 16rpx; font-size: 26rpx; width: 100%; }
.replace-card { background: #f9f9f9; border-radius: 12rpx; padding: 20rpx; margin-bottom: 16rpx; }
.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12rpx; }
.card-title { font-size: 26rpx; font-weight: 500; }
.btn-remove { color: #d32f2f; font-size: 32rpx; padding: 0 10rpx; }
.input-sm { border: 1px solid #ddd; border-radius: 8rpx; padding: 12rpx; font-size: 26rpx; width: 100%; box-sizing: border-box; margin-bottom: 10rpx; }
.row { display: flex; gap: 10rpx; margin-bottom: 10rpx; }
.input-half { flex: 1; border: 1px solid #ddd; border-radius: 8rpx; padding: 12rpx; font-size: 26rpx; }
.input-quad { flex: 1; border: 1px solid #ddd; border-radius: 8rpx; padding: 12rpx; font-size: 26rpx; text-align: center; }
.btn-add { background: transparent; color: #007aff; border: 1px dashed #007aff; border-radius: 10rpx; padding: 16rpx; font-size: 26rpx; width: 100%; margin-bottom: 20rpx; }
.picker { border: 1px solid #ddd; border-radius: 10rpx; padding: 16rpx; font-size: 28rpx; background: #fff; }
.btn-submit { background: #007aff; color: #fff; border: none; border-radius: 12rpx; padding: 24rpx; font-size: 30rpx; width: 100%; margin-top: 20rpx; }
.btn-submit[disabled] { opacity: 0.5; }
</style>
