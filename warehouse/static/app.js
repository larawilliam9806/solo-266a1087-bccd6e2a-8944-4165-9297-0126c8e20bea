(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    var input = document.querySelector('input[name="csrf"]');
    return input ? input.value : "";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function renderResult(box, data) {
    box.hidden = false;
    box.textContent = "";
    box.className = "result " + (data.ok ? "ok" : "bad");

    if (data.ok) {
      box.appendChild(el("h3", null, "整批入账成功，归入 " + data.target_month));
      var s = data.summary || {};
      box.appendChild(
        el(
          "p",
          null,
          "共 " + s.rows + " 行：新增 " + s.new + "，更正 " + s.corrected +
            "，沿用当前值 " + s.noop + "。"
        )
      );
      var p = el("p", "muted", "底表已刷新。");
      box.appendChild(p);
      window.setTimeout(function () {
        window.location.href =
          "/?month=" + encodeURIComponent(data.pending_month);
      }, 900);
      return;
    }

    box.appendChild(el("h3", null, data.error || "批次未通过校验"));
    var details = data.details || [];
    if (details.length) {
      var list = el("ul");
      details.forEach(function (item) {
        var text = "第 " + item.line + " 行";
        if (item.ticket_no) text += "（" + item.ticket_no + "）";
        text += "：" + item.message;
        list.appendChild(el("li", null, text));
      });
      box.appendChild(list);
    }
    if (data.details_truncated) {
      box.appendChild(
        el("p", "muted", "仅显示前 " + details.length + " 条，共 " + data.detail_count + " 条。")
      );
    }
    box.appendChild(el("p", "muted", "整批退回，未写入任何数据，原单据与底表保持不变。"));
  }

  var form = document.getElementById("upload-form");
  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var box = document.getElementById("upload-result");
      var button = form.querySelector('button[type="submit"]');
      var fileInput = form.querySelector('input[type="file"]');
      if (!fileInput.files.length) {
        renderResult(box, { ok: false, error: "请选择要上传的 CSV 文件" });
        return;
      }
      var body = new FormData();
      body.append("file", fileInput.files[0]);
      body.append("target_month", form.target_month.value);

      button.disabled = true;
      button.textContent = "校验中…";

      fetch("/api/upload", {
        method: "POST",
        body: body,
        headers: { "X-CSRF-Token": csrfToken(), "Accept": "application/json" },
      })
        .then(function (response) {
          return response.json().then(function (data) {
            if (!response.ok) {
              data.ok = false;
              if (!data.error) data.error = "上传失败（HTTP " + response.status + "）";
            }
            return data;
          });
        })
        .catch(function () {
          return { ok: false, error: "网络中断，未确认本次上传是否生效，请刷新后核对底表" };
        })
        .then(function (data) {
          button.disabled = false;
          button.textContent = "上传并校验整批";
          renderResult(box, data);
          if (data.ok) {
            fileInput.value = "";
          }
        });
    });
  }

  document.querySelectorAll("[data-seal]").forEach(function (button) {
    button.addEventListener("click", function () {
      var month = button.getAttribute("data-seal");
      if (!window.confirm("封存 " + month + " 后底表将不可更改，并自动进入下一个月。确认封存？")) {
        return;
      }
      var form = document.createElement("form");
      form.method = "post";
      form.action = "/seal";
      [["csrf", csrfToken()], ["month", month]].forEach(function (pair) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = pair[0];
        input.value = pair[1];
        form.appendChild(input);
      });
      document.body.appendChild(form);
      form.submit();
    });
  });
})();
