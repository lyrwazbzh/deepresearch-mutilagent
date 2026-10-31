#!/bin/bash

# 行业信息助手 - 一键启动脚本
# 用法: ./start-services.sh [command]
# 命令: start | stop | restart | status | logs

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查 Docker 是否运行
check_docker() {
    if ! docker info > /dev/null 2>&1; then
        log_error "Docker 未运行，请先启动 Docker Desktop"
        exit 1
    fi
    log_success "Docker 运行正常"
}

# 轮询等待全部中间件 healthy（T31：替代固定 sleep 10）。
# 不用 `docker compose wait`：其在部分 compose 版本语义为「等待容器退出」
# 而非「等待 healthy」（票面风险条），按容器名轮询 inspect 的 Health.Status。
wait_for_healthy() {
    local containers=(industry_postgres industry_redis industry_etcd industry_minio industry_milvus industry_elasticsearch)
    local timeout=180 elapsed=0 all_healthy=1 c state
    log_info "等待服务健康检查通过（最长 ${timeout}s）..."
    while [ "$elapsed" -lt "$timeout" ]; do
        all_healthy=1
        for c in "${containers[@]}"; do
            state=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo "missing")
            if [ "$state" != "healthy" ]; then
                all_healthy=0
                break
            fi
        done
        if [ "$all_healthy" -eq 1 ]; then
            log_success "全部中间件已 healthy（${elapsed}s）"
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    log_warning "等待超时（${timeout}s），部分服务未就绪，以下为当前状态："
    check_service_health
    return 1
}

# 启动中间件服务
start_services() {
    log_info "正在启动中间件服务 (PostgreSQL, Redis, Milvus, Elasticsearch)..."
    docker compose up -d

    wait_for_healthy

    # 检查服务状态
    check_service_health

    log_success "所有服务已启动!"
    echo ""
    echo "服务访问地址:"
    echo "  - PostgreSQL: localhost:5432"
    echo "  - Redis: localhost:6379"
    echo "  - Milvus: localhost:19530"
    echo "  - Elasticsearch: localhost:1200"
    echo "  - MinIO Console: localhost:9001 (账号/口令见你配置的 MINIO_ROOT_USER / MINIO_ROOT_PASSWORD)"
    echo ""
    # T30 起后端已随 docker compose 启动（容器占用 :8000，后台构建/启动可能略慢于中间件健康检查）
    log_info "后端已随 compose 启动于 http://localhost:8000（本机调试后端代码时才需要: cd backend && python app/app_main.py，注意先停掉容器避免端口冲突）"
    echo "  - 前端: cd frontend && npm run dev"
}

# 停止服务
stop_services() {
    log_info "正在停止所有中间件服务..."
    docker compose down
    log_success "所有服务已停止"
}

# 重启服务
restart_services() {
    log_warning "重启将短暂中断所有中间件服务。"
    read -p "确定要继续吗? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        log_info "操作已取消"
        return 0
    fi
    stop_services
    sleep 2
    start_services
}

# 检查服务健康状态
check_service_health() {
    log_info "检查服务健康状态..."

    # PostgreSQL
    if docker exec industry_postgres pg_isready -U postgres > /dev/null 2>&1; then
        log_success "PostgreSQL: 运行中"
    else
        log_warning "PostgreSQL: 启动中..."
    fi

    # Redis
    if docker exec industry_redis redis-cli ping > /dev/null 2>&1; then
        log_success "Redis: 运行中"
    else
        log_warning "Redis: 启动中..."
    fi

    # Milvus
    if curl -s http://localhost:9091/healthz > /dev/null 2>&1; then
        log_success "Milvus: 运行中"
    else
        log_warning "Milvus: 启动中..."
    fi

    # Elasticsearch
    if curl -s http://localhost:1200/_cluster/health > /dev/null 2>&1; then
        log_success "Elasticsearch: 运行中"
    else
        log_warning "Elasticsearch: 启动中..."
    fi
}

# 查看服务状态
show_status() {
    log_info "服务状态:"
    docker compose ps
    echo ""
    check_service_health
}

# 查看日志
show_logs() {
    if [ -z "$2" ]; then
        docker compose logs -f --tail=100
    else
        docker compose logs -f --tail=100 "$2"
    fi
}

# 清理数据（危险操作）
clean_data() {
    log_warning "警告: 此操作将删除所有数据，包括数据库、缓存和向量数据!"
    log_warning "具体后果: 执行 docker compose down -v，所有数据卷（postgres/redis/milvus/es 等）将被删除且不可恢复。"
    read -p "确定要继续吗? (yes/no): " confirm
    if [ "$confirm" = "yes" ]; then
        stop_services
        docker compose down -v
        log_success "所有数据已清理"
    else
        log_info "操作已取消"
    fi
}

# 显示帮助
show_help() {
    echo "行业信息助手 - 服务管理脚本"
    echo ""
    echo "用法: $0 [命令]"
    echo ""
    echo "命令:"
    echo "  start    启动所有中间件服务"
    echo "  stop     停止所有服务"
    echo "  restart  重启所有服务"
    echo "  status   查看服务状态"
    echo "  logs     查看服务日志 (可选: logs [服务名])"
    echo "  clean    清理所有数据 (危险!)"
    echo "  help     显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  $0 start          # 启动服务"
    echo "  $0 logs postgres  # 查看 PostgreSQL 日志"
}

# 主逻辑
case "$1" in
    start)
        check_docker
        start_services
        ;;
    stop)
        stop_services
        ;;
    restart)
        check_docker
        restart_services
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs "$@"
        ;;
    clean)
        clean_data
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        show_help
        ;;
esac
