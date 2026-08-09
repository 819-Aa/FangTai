<script setup lang="ts">
import { onMounted } from "vue";
import { Sparkles } from "@lucide/vue";

import ChatPanel from "@/features/chat/ChatPanel.vue";
import ParticipantContext from "@/features/participants/ParticipantContext.vue";
import { useRecommendationStore } from "@/stores/recommendation";

const store = useRecommendationStore();

onMounted(() => {
  void store.loadProfiles();
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
        :profiles="store.profiles"
        :selected-ids="store.selectedIds"
        :loading="store.loadingProfiles"
        :disabled="store.isStreaming"
        @bind="store.bindProfile"
        @remove="store.removeProfile"
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
      :participants="store.selectedProfiles"
      @send="store.send"
    />
  </div>
</template>
