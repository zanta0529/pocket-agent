# Python 發布方式分析：PyInstaller 與 Bundled Python env（2026）

> 本文件整理自對話中的研究報告，保留其比較、案例與來源。存檔時未重新查核網頁最新內容；所列案例不是完整的 2026 年產業調查，也不代表市場占有率統計。

## 摘要

**如果是「功能固定、交給一般使用者執行」的 Python 工具，優先考慮 PyInstaller；如果產品需要外掛、動態安裝套件、AI／GPU 相依環境，則較適合 Bundled Python environment。**

不過兩者並非「有沒有附 Python」的差別：**PyInstaller 也會附帶 Python 直譯器**，真正差別是「凍結應用程式與相依套件」或「保留較完整、可運作的 Python 環境」。公開案例兩種都有採用，不能斷言 2026 年已由其中一種全面取代另一種。

參考：[PyInstaller 官方說明](https://pyinstaller.org/en/stable/operating-mode.html)、[ComfyUI 官方文件](https://docs.comfy.org/installation/comfyui_portable_windows)。

## 一、先釐清兩種發布方式

### PyInstaller：把程式凍結成應用程式

PyInstaller 分析 import，收集程式、Python 直譯器、套件及必要資源，再由 bootloader 啟動。使用者不需要自行安裝 Python。它主要提供：

- **`onedir`**：一個資料夾，內含執行檔與相依檔案。
- **`onefile`**：一個執行檔，執行時將必要檔案解壓到暫存目錄。

**它不是把整個 Python 程式編譯成原生機器碼的編譯器，也不只支援單一 EXE。**

參考：[PyInstaller 運作方式](https://pyinstaller.org/en/stable/operating-mode.html)。

### Bundled Python env：隨產品提供專用 Python 環境

這裡指的是一起交付 Python runtime、套件、程式碼與啟動器，例如：

```text
MyApp/
├─ runtime/       # 專用 Python 與套件
├─ app/           # 應用程式
└─ launcher.exe   # 或其他啟動入口
```

ComfyUI Portable 就是具體例子：附帶 `python_embeded`、ComfyUI 程式目錄及啟動批次檔，解壓後便能執行，也提供對內附環境安裝套件的方式。

**不要將 Bundled env 理解成「直接把開發機的 `.venv` 壓縮給別人」。** Python 官方明確指出，一般 venv 存在基底 Python 與絕對路徑相依性，並非可任意搬移的發布格式。

參考：[ComfyUI Portable](https://docs.comfy.org/installation/comfyui_portable_windows)、[Python venv 文件](https://docs.python.org/3/library/venv.html)。

## 二、優缺點比較

下表的「較適合」屬於根據工具機制與實際案例做出的工程判斷，不是市場占有率統計。

| 比較面向         | PyInstaller                                                                                | Bundled Python env                                                                              |
| ---------------- | ------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| 使用者體驗       | 容易提供直接執行的入口；`onefile` 特別方便單檔交付。                                       | 也能做到解壓即用或安裝即用，不一定要使用者操作 Python。                                         |
| 初期發布成本     | 相依性單純時較省事；但動態 import 與資源檔可能需要 hooks、hidden imports 或 `.spec` 設定。 | 需要安排 runtime、套件與啟動入口；好處是可以保留一般 Python 環境操作方式。                      |
| 外掛與動態相依性 | 可以做外掛，但無法自動偵測所有動態 import，發布後擴充需要額外設計。                        | 較適合需要新增 Python 套件、使用者自訂節點的產品；ComfyUI 就採用此模式。                        |
| 啟動時間         | `onefile` 有解壓成本，官方指出通常比 `onedir` 慢一些。                                     | 預先展開的環境不需要 PyInstaller 式的每次啟動解壓；但不能據此保證一定比 `onedir` 快，仍須實測。 |
| 更新方式         | 不一定每次都得整包更新：`onedir` 在相依性不變時，可以只更新應用程式執行檔。                | 比較自然地拆成程式碼更新、套件更新與 runtime 更新；ComfyUI 提供不同更新入口。                   |
| 除錯與維護       | 要處理 frozen 執行環境及打包遺漏；官方建議先讓 `onedir` 正常，再做 `onefile`。             | 可以直接呼叫內附 Python、檢查或安裝套件；但允許修改環境，也代表需要管理相依性變更風險。         |
| 程式碼保護       | 主要是封裝 bytecode，並非可靠的防逆向機制。                                                | 若直接交付 `.py`，程式碼自然可讀；這是交付格式的結果，不應視為安全邊界。                        |
| 跨平台           | 產物與 OS、Python 版本及架構相關，需要為目標平台分別建置。                                 | 同樣要提供對應平台的 runtime；例如 Thonny 分別提供 Windows／macOS 與不同 CPU 架構的安裝包。     |

比較依據：[PyInstaller 運作方式](https://pyinstaller.org/en/stable/operating-mode.html)、[ComfyUI Portable](https://docs.comfy.org/installation/comfyui_portable_windows)、[Thonny 官網](https://thonny.org/)。

### 兩個容易誤判的地方

#### 1.「Bundled env 一定非常大、PyInstaller 一定很小」並不成立

PyInstaller 會收集分析到的相依性，不代表其結果就是最小；Bundled env 也不代表一定要帶上整個開發環境。實際應比較同功能版本的下載大小、安裝後大小及啟動時間，而非只比較「單一檔案」與「資料夾」。

參考：[PyInstaller 收集機制](https://pyinstaller.org/en/stable/operating-mode.html)。

#### 2.「要給一般使用者，就一定要 `onefile`」也不成立

`onedir` 本來就是官方支援的發布模式，而且較容易診斷問題；`onefile` 則額外帶來暫存解壓、異常結束後殘留檔案，以及部分暫存目錄執行限制。一般桌面產品是否需要單檔，應與「是否需要安裝程式」分開判斷。

參考：[PyInstaller 發布模式](https://pyinstaller.org/en/stable/operating-mode.html)。

## 三、公開案例：知名程式採用哪些做法？

**以下區分到特定發行形式，不把一個專案的所有平台版本都歸為同一類。** 網站文件會持續更新，以下是原報告列出的公開案例，不是完整的「2026 全產業調查」。

| 專案／發行形式                      | 公開資料所述方式                                                                                           | 能得到的啟示                                                          |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| yt-dlp 的部分獨立執行檔             | 官方 Releases 提到 **PyInstaller-bundled executables**；同時也有 zipimport 執行檔、原始碼等其他發行形式。  | 成熟的 CLI 工具仍使用 PyInstaller；不能說它只是原型或小專案的工具。   |
| ComfyUI Portable for Windows        | **Bundled Python env**：附 `python_embeded`，提供 GPU 對應套件、啟動腳本及額外套件安裝方式。               | AI 工具需要自訂節點、GPU 相依性及套件擴充時，保留 Python 環境很實用。 |
| Thonny 的 Windows／macOS 官方安裝包 | **隨安裝包提供 Python**；Windows 另有包含 Python 的 portable 版本。Linux 官方安裝途徑則可使用既有 Python。 | 即使是面向初學者的 GUI 產品，Bundled Python 仍能提供簡單的安裝體驗。  |

案例來源：

- [yt-dlp Releases](https://github.com/yt-dlp/yt-dlp/releases)
- [ComfyUI Portable 官方文件](https://docs.comfy.org/installation/comfyui_portable_windows)
- [Thonny 官網](https://thonny.org/)

因此，比較準確的結論是：

> 不是「知名程式都用哪一種」，而是產品究竟要交付一個固定功能的應用程式，還是一個可以擴充的 Python 執行平台。

本報告沒有足夠資料支持「2026 年多數商業 Python 軟體使用某一種方式」或具體占比；以上案例也不構成統計樣本。

## 四、實務選型建議

### 優先選 PyInstaller 的情境

以下屬於工程建議：

- 功能固定的 CLI、檔案轉換器、桌面管理工具。
- 使用者不需要自行安裝 Python 套件。
- 團隊希望先降低發布工程成本。
- 希望把套件集合固定在每個發行版本中。

**建議起點是 `onedir`，有明確單檔交付需求才再做 `onefile`。** 這也符合官方「先確認單資料夾版本正常」的流程。

參考：[PyInstaller 官方說明](https://pyinstaller.org/en/stable/operating-mode.html)。

### 優先選 Bundled Python env 的情境

以下屬於工程建議：

- AI／ML 桌面工具，需要 Torch、GPU 套件與不同硬體組合。
- 有 Python 外掛、自訂節點或使用者腳本。
- 必須在產品內執行一般 Python 指令或管理額外套件。
- 希望程式碼與大型執行環境採不同更新節奏。

這些需求與 ComfyUI Portable 的公開設計相符，但**有 AI 套件不代表 PyInstaller 一定不可行**；關鍵仍是環境是否需要持續擴充。

### 兩種方案都應納入的發布要求

以下是建議的驗收條件，而不是任一工具自動保證的能力：

1. 在**沒有預先安裝 Python 的乾淨機器**驗證。
2. 明確列出支援的 OS、CPU 架構與 GPU／驅動需求。
3. 固定套件版本，保留可重現的建置流程。
4. 設計更新失敗的回復方式，避免更新到一半的環境。
5. 規劃簽章、授權清單及 Python／套件安全更新。
6. 若有外掛，明確區分核心環境與可變動的擴充環境。

**若只需要一個預設選擇：一般固定功能桌面工具選「PyInstaller `onedir`＋安裝包」；可擴充的 AI／腳本平台選「專用 Python runtime＋受控套件環境＋啟動器」。**

這是依維護需求提出的選型建議，不是根據未經證實的業界流行程度。
