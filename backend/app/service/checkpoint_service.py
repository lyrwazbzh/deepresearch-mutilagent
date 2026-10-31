# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""检查点服务 - 用于保存和恢复深度研究状态"""
import json
import logging
from copy import deepcopy
from typing import Dict, Any, Optional, List
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from models.research import ResearchCheckpoint
from core.database import SessionLocal

logger = logging.getLogger(__name__)


class CheckpointNotFound(Exception):
    """检查点不存在或不属于当前用户。"""


class OutlineApprovalConflict(Exception):
    """检查点状态或大纲版本不允许当前批准操作。"""


class CheckpointService:
    """检查点服务"""

    def __init__(self):
        pass

    def _get_db(self) -> Session:
        """获取数据库会话"""
        return SessionLocal()

    def save_checkpoint(
        self,
        session_id: str,
        state: Dict[str, Any],
        user_id: Optional[str] = None,
        ui_state: Optional[Dict[str, Any]] = None,
        final_report: Optional[str] = None,
        status: str = "running",
    ) -> Optional[str]:
        """
        保存检查点

        Args:
            session_id: 研究会话 ID
            state: ResearchState 字典（后端状态）
            user_id: 用户 ID（可选）
            ui_state: 前端 UI 状态（研究步骤、搜索结果、图表等）
            final_report: 最终报告内容

        Returns:
            检查点 ID，失败返回 None
        """
        db = self._get_db()
        try:
            # 提取关键信息
            query = state.get("query", "")
            phase = state.get("phase", "planning")
            iteration = state.get("iteration", 0)

            # 清理 state 中不可序列化的内容
            clean_state = self._clean_state_for_storage(state)
            clean_ui_state = self._clean_state_for_storage(ui_state) if ui_state else None

            # 查找现有检查点
            existing = db.query(ResearchCheckpoint).filter(
                ResearchCheckpoint.session_id == session_id
            ).first()

            if existing:
                # 更新现有检查点
                existing.phase = phase
                existing.iteration = iteration
                existing.state_json = clean_state
                if clean_ui_state:
                    existing.ui_state_json = clean_ui_state
                if final_report:
                    existing.final_report = final_report
                existing.status = status
                existing.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                checkpoint_id = str(existing.id)
            else:
                # 创建新检查点
                checkpoint = ResearchCheckpoint(
                    session_id=session_id,
                    user_id=UUID(user_id) if user_id else None,
                    query=query,
                    phase=phase,
                    iteration=iteration,
                    state_json=clean_state,
                    ui_state_json=clean_ui_state,
                    final_report=final_report,
                    status=status,
                )
                db.add(checkpoint)
                db.flush()
                checkpoint_id = str(checkpoint.id)

            db.commit()
            # 详细日志
            ui_steps = clean_ui_state.get("research_steps", []) if clean_ui_state else []
            ui_search = clean_ui_state.get("search_results", []) if clean_ui_state else []
            ui_charts = clean_ui_state.get("charts", []) if clean_ui_state else []
            ui_kg = clean_ui_state.get("knowledge_graph", {}) if clean_ui_state else {}
            logger.info(f"[CheckpointService] 保存成功: session={session_id}, phase={phase}, "
                       f"ui_state=[steps={len(ui_steps)}, search_results={len(ui_search)}, "
                       f"charts={len(ui_charts)}, kg_nodes={len(ui_kg.get('nodes', []) if ui_kg else [])}]")
            return checkpoint_id

        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            db.rollback()
            return None
        finally:
            db.close()

    def load_checkpoint(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        加载最新的检查点（仅后端状态）

        Args:
            session_id: 研究会话 ID

        Returns:
            ResearchState 字典，未找到返回 None
        """
        db = self._get_db()
        try:
            query = db.query(ResearchCheckpoint).filter(
                ResearchCheckpoint.session_id == session_id
            )
            if user_id:
                query = query.filter(ResearchCheckpoint.user_id == UUID(user_id))
            checkpoint = query.order_by(ResearchCheckpoint.updated_at.desc()).first()

            if not checkpoint:
                return None

            return checkpoint.state_json

        except Exception as e:
            logger.error(f"Failed to load checkpoint: {e}")
            return None
        finally:
            db.close()

    def load_full_checkpoint(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        加载完整的检查点（包含后端状态、UI状态和报告）

        Args:
            session_id: 研究会话 ID

        Returns:
            完整检查点数据，包含 state_json, ui_state_json, final_report 等
        """
        db = self._get_db()
        try:
            query = db.query(ResearchCheckpoint).filter(
                ResearchCheckpoint.session_id == session_id
            )
            if user_id:
                query = query.filter(ResearchCheckpoint.user_id == UUID(user_id))
            checkpoint = query.order_by(ResearchCheckpoint.updated_at.desc()).first()

            if not checkpoint:
                logger.info(f"[CheckpointService] 未找到检查点: session={session_id}")
                return None

            result = checkpoint.to_dict(include_state=True)
            # 详细日志
            ui_state = result.get("ui_state_json", {})
            if ui_state:
                logger.info(f"[CheckpointService] 加载成功: session={session_id}, phase={result.get('phase')}, "
                           f"ui_state=[steps={len(ui_state.get('research_steps', []))}, "
                           f"search_results={len(ui_state.get('search_results', []))}, "
                           f"charts={len(ui_state.get('charts', []))}, "
                           f"kg_nodes={len((ui_state.get('knowledge_graph') or {}).get('nodes', []))}]")
            else:
                logger.info(f"[CheckpointService] 加载成功但无ui_state: session={session_id}, phase={result.get('phase')}")
            return result

        except Exception as e:
            logger.error(f"Failed to load full checkpoint: {e}")
            return None
        finally:
            db.close()

    def get_checkpoint_info(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        获取检查点信息（不包含完整状态）

        Args:
            session_id: 研究会话 ID

        Returns:
            检查点元信息
        """
        db = self._get_db()
        try:
            query = db.query(ResearchCheckpoint).filter(
                ResearchCheckpoint.session_id == session_id
            )
            if user_id:
                query = query.filter(ResearchCheckpoint.user_id == UUID(user_id))
            checkpoint = query.order_by(ResearchCheckpoint.updated_at.desc()).first()

            if not checkpoint:
                return None

            return checkpoint.to_dict()

        except Exception as e:
            logger.error(f"Failed to get checkpoint info: {e}")
            return None
        finally:
            db.close()

    def list_checkpoints(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        列出检查点

        Args:
            user_id: 用户 ID（可选，用于过滤）
            status: 状态过滤
            limit: 限制数量

        Returns:
            检查点列表
        """
        db = self._get_db()
        try:
            query = db.query(ResearchCheckpoint)

            if user_id:
                query = query.filter(ResearchCheckpoint.user_id == UUID(user_id))
            if status:
                query = query.filter(ResearchCheckpoint.status == status)

            checkpoints = query.order_by(
                ResearchCheckpoint.updated_at.desc()
            ).limit(limit).all()

            return [cp.to_dict() for cp in checkpoints]

        except Exception as e:
            logger.error(f"Failed to list checkpoints: {e}")
            return []
        finally:
            db.close()

    def update_status(
        self,
        session_id: str,
        status: str,
        error_message: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """
        更新检查点状态

        Args:
            session_id: 研究会话 ID
            status: 新状态 (running/paused/completed/failed)
            error_message: 错误信息（可选）

        Returns:
            是否成功
        """
        db = self._get_db()
        try:
            query = db.query(ResearchCheckpoint).filter(
                ResearchCheckpoint.session_id == session_id
            )
            if user_id:
                query = query.filter(ResearchCheckpoint.user_id == UUID(user_id))
            checkpoint = query.first()

            if not checkpoint:
                return False

            checkpoint.status = status
            if error_message:
                checkpoint.error_message = error_message
            checkpoint.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

            db.commit()
            return True

        except Exception as e:
            logger.error(f"Failed to update checkpoint status: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def approve_outline(
        self,
        session_id: str,
        user_id: str,
        outline_revision: str,
        sections: List[Dict[str, Any]],
        research_questions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """锁定并原子批准当前用户的待审核研究计划。"""
        db = self._get_db()
        try:
            checkpoint = (
                db.query(ResearchCheckpoint)
                .filter(
                    ResearchCheckpoint.session_id == session_id,
                    ResearchCheckpoint.user_id == UUID(user_id),
                )
                .with_for_update()
                .first()
            )
            if not checkpoint:
                raise CheckpointNotFound(session_id)

            state = deepcopy(checkpoint.state_json or {})
            if (
                checkpoint.phase != "awaiting_outline_approval"
                or checkpoint.status != "paused"
            ):
                raise OutlineApprovalConflict(
                    "Research is not waiting for outline approval"
                )
            if state.get("outline_revision") != outline_revision:
                raise OutlineApprovalConflict("Outline revision is stale")

            normalized_sections = self._validate_approved_sections(sections)
            normalized_questions = self._validate_approved_questions(
                research_questions
            )
            state["outline"] = normalized_sections
            state["research_questions"] = normalized_questions
            state["phase"] = "planning"

            checkpoint.state_json = self._clean_state_for_storage(state)
            checkpoint.phase = "planning"
            checkpoint.status = "running"
            checkpoint.error_message = None
            checkpoint.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            return deepcopy(checkpoint.state_json)
        except (CheckpointNotFound, OutlineApprovalConflict, ValueError):
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception(
                "Failed to approve outline for session %s",
                session_id,
            )
            raise
        finally:
            db.close()

    def _validate_approved_sections(
        self,
        sections: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not 3 <= len(sections) <= 12:
            raise ValueError("Outline must contain between 3 and 12 sections")

        normalized = []
        used_ids = set()
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                raise ValueError("Every outline section must be an object")
            section_id = str(section.get("id", "")).strip()
            title = str(section.get("title", "")).strip()
            description = str(section.get("description", "")).strip()
            if not section_id or not title or not description:
                raise ValueError("Section id, title and description are required")
            if section_id in used_ids:
                raise ValueError(f"Duplicate section id: {section_id}")
            used_ids.add(section_id)
            normalized.append({
                "id": section_id,
                "title": title,
                "description": description,
                "section_type": section.get("section_type", "mixed"),
                "requires_data": bool(section.get("requires_data", False)),
                "requires_chart": bool(section.get("requires_chart", False)),
                "priority": index + 1,
                "search_queries": [],
                "status": "pending",
            })
        return normalized

    def _validate_approved_questions(
        self,
        questions: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        if not 3 <= len(questions) <= 12:
            raise ValueError(
                "Research plan must contain between 3 and 12 questions"
            )

        normalized = []
        used_ids = set()
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError("Every research question must be an object")
            question_id = str(question.get("id", "")).strip()
            text = str(question.get("text", "")).strip()
            if not question_id or not text:
                raise ValueError("Research question id and text are required")
            if question_id in used_ids:
                raise ValueError(f"Duplicate research question id: {question_id}")
            used_ids.add(question_id)
            normalized.append({"id": question_id, "text": text})
        return normalized

    def delete_checkpoint(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> bool:
        """
        删除检查点

        Args:
            session_id: 研究会话 ID

        Returns:
            是否成功
        """
        db = self._get_db()
        try:
            query = db.query(ResearchCheckpoint).filter(
                ResearchCheckpoint.session_id == session_id
            )
            if user_id:
                query = query.filter(ResearchCheckpoint.user_id == UUID(user_id))
            deleted = query.delete()

            db.commit()
            return deleted > 0

        except Exception as e:
            logger.error(f"Failed to delete checkpoint: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def _clean_state_for_storage(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        清理状态以便存储

        移除不可序列化的内容，保留可恢复的数据
        """
        unsupported = object()

        def clean_value(value: Any):
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            if isinstance(value, UUID):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    key_text = str(key)
                    if key_text.startswith("_"):
                        continue
                    cleaned_item = clean_value(item)
                    if cleaned_item is not unsupported:
                        result[key_text] = cleaned_item
                return result
            if isinstance(value, (list, tuple)):
                result = []
                for item in value:
                    cleaned_item = clean_value(item)
                    if cleaned_item is not unsupported:
                        result.append(cleaned_item)
                return result
            return unsupported

        clean = clean_value(state)
        if clean is unsupported or not isinstance(clean, dict):
            clean = {}
        json.dumps(clean)
        return clean


# 单例
_checkpoint_service = None


def get_checkpoint_service() -> CheckpointService:
    """获取检查点服务实例"""
    global _checkpoint_service
    if _checkpoint_service is None:
        _checkpoint_service = CheckpointService()
    return _checkpoint_service
