<script setup lang="ts">
import { computed } from 'vue'
import { useMeta } from '@/stores/meta'
import PageHead from '@/components/PageHead.vue'
import AppIcon from '@/components/AppIcon.vue'
import wechat from '@/assets/sponsor/wechat.jpg'
import alipay from '@/assets/sponsor/alipay.jpg'
import community from '@/assets/sponsor/community.jpg'

const meta = useMeta()
const version = computed(() => meta.version || '1.1.0')

const links = [
  {
    title: '项目主页',
    detail: '源码、版本与更新记录',
    href: 'https://github.com/astralwaveorg/lyra',
    icon: 'Link',
  },
  {
    title: '使用文档',
    detail: '安装、配置、备份与常见问题',
    href: 'https://astralwaveorg.github.io/lyra/',
    icon: 'Document',
  },
  {
    title: '问题反馈',
    detail: 'Bug、功能建议与使用疑问',
    href: 'https://github.com/astralwaveorg/lyra/issues',
    icon: 'Warning',
  },
]
</script>

<template>
  <PageHead :count="`v${version}`" />

  <section class="about-hero card">
    <div class="hero-mark"><AppIcon name="Headset" :size="22" /></div>
    <div class="hero-text">
      <h2>Lyra</h2>
      <p>
        自托管的音乐库维护工具。补齐歌词与标签、按规则整理目录、检查重复音频，
        所有写入都留有备份与撤销记录。
      </p>
      <div class="hero-meta dim">版本 {{ version }} · MIT License</div>
    </div>
  </section>

  <div class="link-grid">
    <a v-for="item in links" :key="item.href" class="card link-card" :href="item.href" target="_blank" rel="noreferrer">
      <span class="link-icon"><AppIcon :name="item.icon" :size="17" /></span>
      <span class="link-text">
        <strong>{{ item.title }}</strong>
        <span class="dim">{{ item.detail }}</span>
      </span>
      <AppIcon name="ArrowRight" :size="15" class="link-arrow" />
    </a>
  </div>

  <section class="card">
    <div class="card-head">
      <span class="card-title">赞赏</span>
      <span class="card-hint">完全自愿，不影响任何功能</span>
    </div>
    <div class="card-body">
      <p class="prose">
        如果 Lyra 帮你省下了整理曲库的时间，可以请作者喝杯咖啡。
        你的支持会用于维护服务器和后续开发。
      </p>
      <div class="qr-grid">
        <figure class="qr-card">
          <img :src="wechat" alt="微信赞赏码" loading="lazy" />
          <figcaption>微信</figcaption>
        </figure>
        <figure class="qr-card">
          <img :src="alipay" alt="支付宝赞赏码" loading="lazy" />
          <figcaption>支付宝</figcaption>
        </figure>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">交流群</span>
      <span class="card-hint">二维码有效期至 2026-10-26</span>
    </div>
    <div class="card-body community">
      <img :src="community" alt="Lyra 官方交流群二维码" loading="lazy" />
      <div class="prose">
        <p>扫码加入 Lyra 官方交流群，反馈问题、交流部署经验。</p>
        <p class="dim">
          二维码过期后请在 GitHub Issues 中反馈，作者会更新。
          群里不提供破解、盗版资源或付费代整理服务。
        </p>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><span class="card-title">许可与致谢</span></div>
    <div class="card-body prose">
      <p>
        Lyra 以 MIT 许可证开源，版权归 Lyra contributors 所有。
        你可以自由使用、修改和分发，但需保留版权与许可声明。
      </p>
      <p class="dim">
        感谢 MusicBrainz、AcoustID、LRCLIB 以及所有提交问题和代码的贡献者。
        本项目与上述服务没有隶属关系。
      </p>
    </div>
  </section>
</template>

<style scoped>
.about-hero {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  padding: 22px;
}

.hero-mark {
  width: 46px;
  height: 46px;
  flex: none;
  border-radius: 13px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
  border: 1px solid var(--accent-line);
}

.hero-text h2 {
  font-size: 20px;
  letter-spacing: 0.02em;
}

.hero-text p {
  margin-top: 6px;
  max-width: 720px;
  color: var(--text-2);
  font-size: 13.5px;
  line-height: 1.75;
}

.hero-meta {
  margin-top: 10px;
  font-size: 12px;
}

.link-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

.link-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 15px 16px;
  color: inherit;
  transition: border-color 0.16s, transform 0.16s;
}

.link-card:hover {
  border-color: var(--accent-line);
  transform: translateY(-1px);
}

.link-icon {
  width: 34px;
  height: 34px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: var(--surface-2);
  color: var(--accent);
}

.link-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.link-text strong {
  font-size: 13.5px;
  font-weight: 600;
}

.link-text .dim {
  font-size: 12px;
}

.link-arrow {
  margin-left: auto;
  color: var(--text-3);
}

.prose {
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.8;
}

.prose p + p {
  margin-top: 8px;
}

.qr-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin-top: 16px;
}

.qr-card {
  margin: 0;
  width: 220px;
  max-width: 100%;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: #fff;
}

.qr-card img {
  display: block;
  width: 100%;
  height: auto;
  border-radius: 6px;
}

.qr-card figcaption {
  padding-top: 8px;
  text-align: center;
  font-size: 12px;
  color: #5c6577;
}

.community {
  display: flex;
  gap: 20px;
  align-items: flex-start;
}

.community img {
  width: 220px;
  max-width: 100%;
  flex: none;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: #fff;
}

@media (max-width: 760px) {
  .link-grid {
    grid-template-columns: 1fr;
  }

  .about-hero {
    padding: 18px;
  }

  .qr-grid {
    gap: 12px;
  }

  .qr-card {
    width: calc(50% - 6px);
  }

  .community {
    flex-direction: column;
    align-items: stretch;
  }

  .community img {
    width: 100%;
    max-width: 260px;
  }
}
</style>
