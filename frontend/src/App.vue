<script setup lang="ts">
import { onMounted } from "vue";
import { Sparkles } from "@lucide/vue";

import ChatPanel from "@/features/chat/ChatPanel.vue";
import ParticipantContext from "@/features/participants/ParticipantContext.vue";
import { useRecommendationStore } from "@/stores/recommendation";

const store = useRecommendationStore();

onMounted(() => {
  // 复用持久化 session_id（不创建无关新会话）；默认一个匿名成员
  void store.ensureSession();
  if (!store.selectedRefs.length) store.addSlot();
});
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
      :status="store.status"
      :is-streaming="store.isStreaming"
      :can-send="store.canSend"
      :participants="store.selectedSlots"
      @send="store.send"
    />
  </div>
</template>
