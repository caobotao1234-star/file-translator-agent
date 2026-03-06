# core/agent.py
import json
from typing import Generator, List
from core.llm_engine import ArkLLMEngine
from core.memory import ConversationMemory
from tools.base_tool import BaseTool
from prompts.system_prompts import AGENT_SYSTEM_PROMPT 

class BaseAgent:
    def __init__(self, llm_engine: ArkLLMEngine, tools: List[BaseTool]):
        self.llm = llm_engine
        self.tools_map = {tool.name: tool for tool in tools}
        self.api_tools = [tool.get_api_format() for tool in tools]
        
        # 【修改】：将硬编码的 simple_prompt 替换为导入的 AGENT_SYSTEM_PROMPT
        self.memory = ConversationMemory(
            system_prompt=AGENT_SYSTEM_PROMPT, 
            llm_engine=self.llm, 
        )
        
        self.total_tokens = 0
        self.total_prompt_tokens = 0      
        self.total_completion_tokens = 0  

    def chat(self, user_input: str) -> Generator[str, None, None]:
        self.memory.add_user_message(user_input)

        # 调试信息打印...
        print("\n" + "▼"*20 + "[Debug: 真正发送给模型的上下文] " + "▼"*20)
        print(self.memory.get_debug_info())
        print("▲"*65 + "\n")

        
        # 【新增】：本次对话的精细 Token 账本
        turn_tokens = 0 
        turn_prompt_tokens = 0
        turn_completion_tokens = 0

        # 【新增】：设定最大思考轮数，防止破产
        max_loops = 15 
        current_loop = 0

        # 【修改】：不再是 while True，而是加了条件
        while current_loop < max_loops:
            current_loop += 1
            
            full_response = ""
            tool_calls_this_turn =[]
            
            for chunk in self.llm.stream_chat(self.memory.get_messages(), tools=self.api_tools):
                if chunk["type"] == "text":
                    full_response += chunk["content"]
                    yield chunk["content"]
                elif chunk["type"] == "tool_call":
                    tool_calls_this_turn.append(chunk)
                elif chunk["type"] == "usage":
                    turn_prompt_tokens += chunk["prompt_tokens"]
                    turn_completion_tokens += chunk["completion_tokens"]
                    turn_tokens += chunk["total_tokens"]
                    self.total_prompt_tokens += chunk["prompt_tokens"]
                    self.total_completion_tokens += chunk["completion_tokens"]
                    self.total_tokens += chunk["total_tokens"]

            if tool_calls_this_turn:
                # --- 【新增步骤 A】：记录 Assistant 发起的工具调用请求 ---
                # 构造符合 API 标准的 tool_calls 列表格式
                api_tool_calls =[
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"]
                        }
                    } for tc in tool_calls_this_turn
                ]
                # 存入记忆（注意：如果模型在此之前输出了思考文本，把 full_response 也带上）
                self.memory.add_assistant_tool_call(tool_calls=api_tool_calls, content=full_response)
                
                # --- 【新增步骤 B】：依次执行工具，并记录标准的 Tool 消息 ---
                for tc in tool_calls_this_turn:
                    action_id = tc["id"]
                    action_name = tc["name"]
                    action_args_str = tc["arguments"]
                    yield f"\n\n⚙️[原生 API 动作]: 模型请求调用[{action_name}]，参数: {action_args_str}...\n"
                    
                    # 【核心改变：智能纠错拦截机制】
                    tool_result = ""
                    try:
                        # 1. 尝试解析参数（如果解析失败，直接跳到 except）
                        action_params = json.loads(action_args_str) if action_args_str else {}
                        
                        # 2. 检查工具是否存在
                        if action_name not in self.tools_map:
                            tool_result = f"系统错误：未知的工具 '{action_name}'。请检查系统工具箱中提供的可用工具列表。"
                        else:
                            tool = self.tools_map[action_name]
                            
                            # 3. 【重点】安检员上岗：校验必填参数
                            is_valid, error_msg = tool.validate_params(action_params)
                            if not is_valid:
                                # 明确告诉模型缺了什么，它下次循环就会补上！
                                tool_result = f"调用失败：{error_msg}。请更正参数后重新调用该工具。"
                            else:
                                # 4. 安检通过，真正执行工具
                                tool_result = tool.execute(action_params)
                                
                    except json.JSONDecodeError:
                        tool_result = f"调用失败：参数不是合法的 JSON 格式 ({action_args_str})。请输出标准的 JSON 后重试。"
                    except Exception as e:
                        # 捕捉工具内部写的代码报错（比如除以 0、网络断开等）
                        tool_result = f"执行失败，工具内部抛出异常：{str(e)}。你可以尝试更换参数重试，或告知用户无法完成。"
                        
                    yield f" 拿到结果（或反馈）：{tool_result}\n"
                    
                    # 不管是成功的结果，还是报错的信息，都必须作为 role="tool" 存入记忆反馈给大模型！
                    self.memory.add_tool_message(
                        tool_call_id=action_id, 
                        name=action_name, 
                        content=str(tool_result) 
                    )
                
                # 防御逻辑：依然保留，防止死循环
                if current_loop == max_loops:
                    yield "\n⚠️[系统警告]: 思考次数超限，强制停止深入思考。\n"
                    # 这里是真正的系统干预，用 user 角色发警告是合理的
                    self.memory.add_user_message("系统警告：工具调用次数已达上限，请立即基于现有信息给出最终结论，严禁再次调用工具！")
                else:
                    yield "\n🧠[Agent]: 正在综合处理所有结果...\n\n"
                    
                continue

            if not tool_calls_this_turn:
                self.memory.add_ai_message(full_response)
                yield f"\n\n[📊 Token 消耗] 本次消耗: {turn_tokens} (输入: {turn_prompt_tokens}, 输出: {turn_completion_tokens}) | 累计消耗: {self.total_tokens} (输入: {self.total_prompt_tokens}, 输出: {self.total_completion_tokens})\n"
                break