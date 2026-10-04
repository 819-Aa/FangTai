<script setup lang="ts">
import { MessageSquare, Plus } from "@lucide/vue";
import type { ChatRecord } from "@/stores/recommendation";

defineProps<{
  chats: ChatRecord[];
  activeChatId: string;
  disabled: boolean;
  storageError: string;
}>();

const emit = defineEmits<{
  newChat: [];
  openChat: [id: string];
}>();
</script>

<template>
  <aside class="chat-sidebar" aria-label="对话列表">
    <div class="sidebar-top">
      <div class="sidebar-heading">对话</div>
      <button class="sidebar-new-chat" type="button" :disabled="disabled" @click="emit('newChat')">
        <Plus :size="16" /> 新对话
      </button>
    </div>
    <div class="sidebar-list" role="navigation" aria-label="历史对话">
      <p v-if="!chats.length" class="sidebar-empty">还没有历史对话</p>
      <button
        v-for="chat in chats"
        :key="chat.id"
        type="button"
        class="sidebar-chat"
        :class="{ active: chat.id === activeChatId }"
        :aria-current="chat.id === activeChatId ? 'page' : undefined"
        :disabled="disabled"
        @click="emit('openChat', chat.id)"
      >
        <MessageSquare :size="16" class="sidebar-chat-icon" />
        <span class="sidebar-chat-text">
          <span class="sidebar-chat-title">{{ chat.title }}</span>
          <span class="sidebar-chat-meta">{{ chat.selectedRefs.length }} 位成员</span>
        </span>
      </button>
    </div>
    <p v-if="storageError" class="sidebar-storage-error" role="alert">{{ storageError }}</p>
  </aside>
</template>
