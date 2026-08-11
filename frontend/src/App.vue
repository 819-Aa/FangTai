<script setup lang="ts">
import { Sparkles } from "@lucide/vue";

import ChatPanel from "@/features/chat/ChatPanel.vue";
import ParticipantContext from "@/features/participants/ParticipantContext.vue";
import { useRecommendationStore } from "@/stores/recommendation";

const store = useRecommendationStore();

// 挂载时不再创建空参与者 session；首次发送时按当前选中参与者创建。
// 默认提供一个匿名成员（从持久化 session 关联组合恢复）。
</script>

<template>
  <div class="app-frame">
    <header class="app-header">
      <div class="brand">
        <span class="brand-icon"><Sparkles :size="18" /></span>
        <span>健康菜品推荐系统 V2</span>
      </div>
      <ParticipantContext
        :slots="store.slots"
        :selected-refs="store.selectedRefs"
        :disabled="store.isStreaming"
        @add="store.addSlot"
        @remove="store.removeSlot"
      />
    </header>

    <div v-if="store.error" class="global-error" role="alert">{{ store.error }}</div>

    <ChatPanel
      class="chat-host"
      :messages="store.messages"
      :phases="store.phases"
      :answer="store.answer"
      :clarification="store.clarification"
      :status="store.status"
      :is-streaming="store.isStreaming"
      :can-send="store.canSend"
      :participants="store.selectedSlots"
      @send="store.send"
    />
  </div>
</template>
