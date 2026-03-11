# core/orchestrator.py
import json
from typing import Generator, List, Dict

from core.agent import BaseAgent
from core.llm_engine import ArkLLMEngine
from core.agent_config import AgentConfig
from core.agent_events import AgentEvent
from tools.base_tool import BaseTool

# =============================================================
# 📘 教学笔记：调度员 Agent（Orchestrator）
# =============================================================
# 多 Agent 架构的核心思想：
#
#   用户 → 调度员 → 选择合适的子 Agent → 子 Agent 执行 → 结果返回给调度员 → 调度员整合后回复用户
#
# 调度员本身也是一个 Agent（继承 BaseAgent），但它有一个特殊工具：
#   delegate_to_agent — 把任务委派给指定的子 Agent
#
# 这个工具的参数是：
#   - agent_name: 要调用哪个子 Agent
#   - task: 具体要这个子 Agent 做什么
#
# 调度员的 LLM 会根据用户的问题，自动决定：
#   1. 这个问题需要哪个专家来处理？
#   2. 应该怎么描述任务给这个专家？
#   3. 专家返回结果后，需不需要进一步加工？
#
# 为什么调度员自己也是 Agent？
#   - 复用 BaseAgent 的所有能力（记忆、重试、持久化等）
#   - 调度员也可以有自己的工具（比如 delegate_to_agent）
#   - 调度员也可以直接回答简单问题，不一定每次都要派子 Agent
# =============================================================


class DelegateToAgentTool(BaseTool):
    """
    特殊工具：委派任务给子 Agent。
    
    这个工具不像普通工具那样自己执行逻辑，
    而是由 OrchestratorAgent 拦截并路由到对应的子 Agent。
    """
    name = "delegate_to_agent"
    description = ""  # 会在 Orchestrator 初始化时动态生成
    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "要委派的子 Agent 名称"
            },
            "task": {
                "type": "string",
                "description": "要委派给子 Agent 的具体任务描述，尽量详细"
            }
        },
        "required": ["agent_name", "task"]
    }

    def execute(self, params: dict) -> str:
        # 实际执行在 OrchestratorAgent 中拦截处理，这里不会被调用
        return "此工具由调度员内部处理"


class OrchestratorAgent(BaseAgent):
    """
    调度员 Agent：分析用户意图，委派任务给合适的子 Agent。
    """

    def __init__(
        self,
        llm_engine: ArkLLMEngine,
        agent_registry: List[Dict],
        config: AgentConfig | None = None,
        session_id: str | None = None,
    ):
        # 📘 根据注册表，构建子 Agent 实例
        self.sub_agents: Dict[str, BaseAgent] = {}
        agent_descriptions = []

        for agent_def in agent_registry:
            name = agent_def["name"]
            # 实例化每个子 Agent 的工具
            tool_instances = [ToolClass() for ToolClass in agent_def.get("tools", [])]

            sub_agent = BaseAgent(
                llm_engine=llm_engine,
                tools=tool_instances,
                config=AgentConfig(
                    max_loops=config.max_loops if config else 8,
                    debug=config.debug if config else False,
                    show_usage=False,  # 子 Agent 不单独显示 usage
                    enable_persistence=False,  # 子 Agent 不需要持久化
                ),
                system_prompt=agent_def["system_prompt"],
                agent_name=name,
            )
            self.sub_agents[name] = sub_agent
            agent_descriptions.append(f"  - {name}: {agent_def['description']}")

        # 📘 动态生成 delegate 工具的描述，让调度员 LLM 知道有哪些子 Agent 可用
        delegate_tool = DelegateToAgentTool()
        delegate_tool.description = (
            "将任务委派给专业的子 Agent 执行。可用的子 Agent 列表：\n"
            + "\n".join(agent_descriptions)
            + "\n\n请根据用户的问题选择最合适的 Agent，并用清晰的语言描述任务。"
        )

        # 📘 构建调度员自己的 system prompt
        orchestrator_prompt = f"""你是一个智能任务调度员（Orchestrator）。

【你的职责】
你负责理解用户的意图，然后决定：
1. 如果是简单的寒暄、闲聊，你可以直接回答，不需要委派。
2. 如果需要专业能力（翻译、编程、信息查询、数学计算），使用 delegate_to_agent 工具委派给合适的子 Agent。
3. 子 Agent 返回结果后，你可以直接转述，也可以加工整合后再回复用户。

【可用的专业 Agent】
{chr(10).join(agent_descriptions)}

【工作原则】
1. 精准匹配：根据任务类型选择最合适的 Agent，不要乱派。
2. 任务描述要清晰：传给子 Agent 的 task 要包含用户的完整需求，不要丢失信息。
3. 如果一个问题涉及多个领域，可以依次委派给多个 Agent，最后整合结果。
4. 你自己也有能力直接回答一般性问题，不要什么都委派。
"""

        # 调用父类构造函数
        super().__init__(
            llm_engine=llm_engine,
            tools=[delegate_tool],
            config=config,
            session_id=session_id,
            system_prompt=orchestrator_prompt,
            agent_name="orchestrator",
        )

    def _execute_tool(self, action_name: str, action_params: dict) -> str:
        """
        覆写父类的工具执行方法，拦截 delegate_to_agent 调用。
        
        📘 当调度员的 LLM 决定调用 delegate_to_agent 时：
        1. 从参数中取出 agent_name 和 task
        2. 找到对应的子 Agent
        3. 调用子 Agent 的 run() 方法执行任务
        4. 把子 Agent 的结果返回给调度员的 LLM
        """
        if action_name == "delegate_to_agent":
            agent_name = action_params.get("agent_name", "")
            task = action_params.get("task", "")

            if agent_name not in self.sub_agents:
                available = ", ".join(self.sub_agents.keys())
                return f"错误：未知的 Agent '{agent_name}'。可用的 Agent: {available}"

            if self.config.debug:
                print(f"\n[🔀 调度员]: 委派任务给 [{agent_name}] → \"{task}\"")

            sub_agent = self.sub_agents[agent_name]
            result = sub_agent.run(task)

            if self.config.debug:
                print(f"[🔀 调度员]: [{agent_name}] 返回结果（{len(result)} 字符）")

            return f"[{agent_name} 的回复]:\n{result}"

        # 非委派工具，走父类的默认逻辑
        return super()._execute_tool(action_name, action_params)
