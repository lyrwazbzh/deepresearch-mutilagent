/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { lazy, Suspense } from 'react'
import { BarChartOutlined, PictureOutlined } from '@ant-design/icons'
import PageLoading from '@/components/page-loading'
import styles from './visualization.module.scss'

// T35：懒加载 echarts-for-react（本文件是第三个静态引入点，批次 12 审查补修）
const ReactECharts = lazy(() => import('echarts-for-react'))

interface ChartConfig {
  id: string
  title: string
  subtitle?: string
  type: 'line' | 'bar' | 'pie' | 'horizontal_bar' | 'radar' | 'sankey' | 'wordcloud' | 'graph'
  echarts_option?: Record<string, unknown>
  image_base64?: string  // 支持 matplotlib 生成的图片
}

interface VisualizationProps {
  charts?: ChartConfig[]
}

export default function Visualization({ charts }: VisualizationProps) {
  if (!charts?.length) {
    return (
      <div className={styles.empty}>
        <BarChartOutlined className={styles.emptyIcon} />
        <span>暂无可视化图表</span>
      </div>
    )
  }

  return (
    <div className={styles.grid}>
      {charts.map((chart) => (
        <div key={chart.id} className={styles.card}>
          <div className={styles.cardHeader}>
            <h3 className={styles.cardTitle}>{chart.title}</h3>
            {chart.subtitle && <p className={styles.cardSubtitle}>{chart.subtitle}</p>}
          </div>
          <div className={styles.chartContainer}>
            {chart.image_base64 ? (
              // 渲染 matplotlib 生成的 base64 图片
              <div className={styles.imageWrapper}>
                <img
                  src={`data:image/png;base64,${chart.image_base64}`}
                  alt={chart.title}
                  className={styles.chartImage}
                />
              </div>
            ) : chart.echarts_option ? (
              // 渲染 ECharts 图表
              <Suspense fallback={<PageLoading />}>
                <ReactECharts
                  option={chart.echarts_option}
                  style={{ height: '100%', width: '100%' }}
                  opts={{ renderer: 'canvas' }}
                />
              </Suspense>
            ) : (
              // 无数据占位
              <div className={styles.noData}>
                <PictureOutlined />
                <span>图表数据加载中...</span>
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
