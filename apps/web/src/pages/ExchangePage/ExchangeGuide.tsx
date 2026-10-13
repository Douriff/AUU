/** In-site usage guide for exchange connect (Chinese, plain language). */
export function ExchangeGuide({ primaryIp, standbyIp }: { primaryIp: string; standbyIp: string }) {
  const ips = `${primaryIp} ${standbyIp}`;
  return (
    <section className="settings-section exch-guide" id="guide">
      <h2>使用指南</h2>
      <p className="muted">按顺序做，大约 10 分钟。全程不需要把账号密码给我们，只需要一个“API Key”。</p>

      <details open>
        <summary><b>第 0 步：先了解（很重要）</b></summary>
        <ul>
          <li>API Key 就像一把“只能看账、只能现货买卖”的副钥匙。我们<b>拒绝</b>任何带“提现”权限的 Key，所以通过本站<b>不可能把你的币转走</b>。</li>
          <li>白名单 IP 必须填本站服务器：<b>{primaryIp}</b>（东京，主）和 <b>{standbyIp}</b>（香港，备用）。这样就算 Key 泄露，别人从其他地方也用不了。</li>
          <li>建议先在交易所里只放少量资金测试。</li>
        </ul>
      </details>

      <details>
        <summary><b>第 1 步（Binance 币安）：创建 API Key</b></summary>
        <ol>
          <li>用电脑浏览器登录 binance.com（需要已完成身份认证 KYC）。</li>
          <li>右上角头像 →「API 管理」（Account → API Management）。</li>
          <li>点「创建 API」，选「系统生成」（System generated），起个名字比如 AUUTRADE，完成安全验证（邮箱/手机/谷歌验证码）。</li>
          <li>创建后点「编辑限制」（Edit restrictions）：
            <ul>
              <li>勾选「启用读取」（Enable Reading）——默认已勾选。</li>
              <li>勾选「启用现货及杠杆交易」（Enable Spot &amp; Margin Trading）。</li>
              <li>想用<b>合约</b>：先在币安开通 U 本位合约账户（第一次进入「合约」页面按提示开通并答题），再勾选「允许合约」（Enable Futures）。只用现货可以不勾。</li>
              <li><b>不要</b>勾选「允许提现」（Enable Withdrawals）、「允许万向划转」（Permits Universal Transfer）、「允许内部划转」、杠杆借贷等其他选项。</li>
              <li>IP 访问限制选「仅限受信任的 IP」（Restrict access to trusted IPs only），依次填入 <code>{primaryIp}</code> 和 <code>{standbyIp}</code>，点确认。</li>
            </ul>
          </li>
          <li>保存。把页面上的 <b>API Key</b> 和 <b>Secret Key</b> 复制下来（Secret 只显示一次，丢了就删掉重建）。</li>
        </ol>
        <p className="muted">官方说明：<a href="https://www.binance.com/en/support/faq/how-to-create-api-keys-on-binance-360002502072" target="_blank" rel="noreferrer">How to Create API Keys on Binance</a> · <a href="https://www.binance.com/en/my/settings/api-management" target="_blank" rel="noreferrer">币安 API 管理页</a></p>
      </details>

      <details>
        <summary><b>第 1 步（OKX 欧易）：创建 API Key</b></summary>
        <ol>
          <li>登录 okx.com → 右上角头像 →「API」（API and connections）→「创建 API Key」。手机 App：菜单 → API → 创建 API Key。</li>
          <li>用途选「API 交易」，名字填 AUUTRADE。</li>
          <li>IP 白名单填：<code>{ips}</code>（两个 IP 中间用空格或逗号隔开）。</li>
          <li>权限只勾「读取」和「交易」，<b>不要</b>勾「提现」。（OKX 的「交易」权限同时包含现货和合约。）</li>
          <li>想用合约：OKX 账户模式需要是「合约模式」或「跨币种/组合保证金」，在 交易设置 → 账户模式 里切换（简单模式只能现货）。</li>
          <li>设置一个 <b>Passphrase</b>（口令，自己记住，OKX 找不回），完成安全验证。</li>
          <li>复制 <b>API Key</b>、<b>Secret Key</b>，以及刚设置的 <b>Passphrase</b>。</li>
        </ol>
        <p className="muted">注意：部分地区 OKX 要求账户资产 ≥ 100 USD 才能创建 API Key。官方说明：<a href="https://www.okx.com/help/api-faq" target="_blank" rel="noreferrer">OKX API FAQ</a></p>
      </details>

      <details>
        <summary><b>第 2 步：在 AUUTRADE 绑定</b></summary>
        <ol>
          <li>在本页上方「绑定交易所」里选 Binance 或 OKX。</li>
          <li>粘贴 API Key、Secret（OKX 还要填 Passphrase），点「验证并绑定」。</li>
          <li>系统会自动向交易所查询这个 Key 的权限：有提现权限、或没设 IP 白名单的会被拒绝，并告诉你原因。</li>
          <li>成功后只显示 Key 的前后几位（例如 AbCd…WxYz）。Secret 加密保存，任何页面都不会再显示。</li>
        </ol>
      </details>

      <details>
        <summary><b>第 3 步：下第一笔小单、撤单</b></summary>
        <ol>
          <li>绑定后先点「刷新」看看余额对不对。</li>
          <li>在「下单」里填交易对（如 BTC/USDT），选「限价」，买入价格填一个<b>明显低于现价</b>的价格（例如现价的 90%），数量填很小（金额几 USDT，且要满足交易所最小下单额，通常 5–10 USDT）。</li>
          <li>点「下单」，再勾确认。成功后在「当前挂单」里能看到这笔单（因为价格低，不会马上成交）。</li>
          <li>点这笔单后面的「撤单」，它就消失了。你也可以去交易所 App 里核对一下。</li>
          <li>熟悉后再用市价单或贴近现价的限价单。每笔金额有上限（页面上会显示）。</li>
        </ol>
      </details>

      <details>
        <summary><b>第 3 步（合约）：开仓、设置止盈止损、平仓</b></summary>
        <ol>
          <li>绑定后点上方「合约」标签。第一次会出现<b>合约风险确认</b>，逐条勾选后才能使用。</li>
          <li>填合约（如 BTC/USDT:USDT，直接填 BTC 也行），选「逐仓」（推荐新手）和杠杆（本站有最高杠杆限制，默认 5 倍），点「应用杠杆/模式」。</li>
          <li>填方向（开多/开空）、限价或市价、数量（OKX 单位是「张」，BTC 每张 0.01 BTC），<b>建议同时填止损价</b>，止盈可选。</li>
          <li>点「开仓下单」，在弹窗里看清楚，输入「确认」两个字再提交。</li>
          <li>「合约持仓」会显示开仓价、标记价、<b>强平价</b>、未实现盈亏。点「市价平仓」即可平掉；也可以勾「只减仓」手动下平仓单。</li>
          <li>止盈/止损单会出现在「合约挂单」里，平仓后记得把没用的止盈止损单撤掉。</li>
        </ol>
      </details>

      <details>
        <summary><b>第 4 步：不用了怎么解绑、删除 Key</b></summary>
        <ol>
          <li>在本页点「解绑」：本站立即删除保存的加密 Key。</li>
          <li><b>再去交易所删除这个 API Key</b>（最彻底）：币安 → API 管理 → 删除；OKX → API → 删除。</li>
          <li>如果怀疑 Key 泄露，直接在交易所删除即可，立刻失效。</li>
        </ol>
      </details>

      <details>
        <summary><b>常见问题 &amp; 风险提示</b></summary>
        <ul>
          <li><b>提示“Key 有提现权限”</b>：回交易所编辑这个 Key，取消提现/划转，或新建一个。</li>
          <li><b>提示“没有 IP 白名单” / “交易所拒绝了这个 Key”</b>：检查是否填了 {primaryIp} 和 {standbyIp}，Key/Secret 是否复制完整、没有多余空格；OKX 检查 Passphrase。</li>
          <li><b>“余额不足” / “交易所拒绝订单”</b>：金额太小（低于最小下单额）、价格精度不对或余额不够。</li>
          <li><b>“下单结果未知”</b>：网络抖动。直接再点一次同一笔下单，系统会按同一个订单号去交易所查，不会重复下单。</li>
          <li><b>“紧急停止已开启”</b>：管理员暂停了所有新单，你仍可以撤单。</li>
          <li><b>合约爆仓风险</b>：价格走到「强平价」时交易所会强制平仓。杠杆越高、强平价离现价越近。逐仓最多亏光这个仓位的保证金；全仓可能亏掉合约账户里全部保证金。资金费率每 8 小时左右结算一次（正数时多头付钱给空头）。</li>
          <li><b>提示“没有合约权限”</b>：币安要勾选「允许合约」并先开通合约账户；OKX 要把账户模式切到合约模式。</li>
          <li>只支持现货 XXX/USDT 和 USDT 本位永续合约，不支持币本位合约、期权、提现、划转。本站策略/模拟盘<b>不会</b>自动用你的 Key 下单，所有真实订单都由你自己点击。</li>
          <li>管理员可以开启紧急停止（只能平仓不能开仓），必要时也可以一键帮你平掉所有合约持仓。</li>
          <li>真实交易有亏损风险，市价单可能有滑点；本站不提供投资建议，盈亏由你自己承担。部分国家/地区不允许使用某些交易所，请遵守当地法律和交易所条款。</li>
          <li>服务器在东京，香港是备用节点；节点切换过程中可能短暂只能查看、不能下单。</li>
        </ul>
      </details>
    </section>
  );
}
