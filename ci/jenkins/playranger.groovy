// PlayRanger Jenkins Shared Library
// 用法：
//   @Library('playranger') _
//   playranger(gameUrl: 'http://localhost:8080', goal: '找到所有可复现的缺陷',
//              maxSteps: 50, failOn: 'blocker,critical', allow: 0)
//
// 依赖：agent 上已安装 Python 3.11+ 与 playranger（pip install -e .）

def call(Map config = [:]) {
    def gameUrl = config.get('gameUrl', '')
    def goal = config.get('goal', '找到所有可复现的缺陷')
    def maxSteps = config.get('maxSteps', 50)
    def failOn = config.get('failOn', 'blocker,critical')
    def allow = config.get('allow', 0)
    def outDir = config.get('outDir', 'output/ci')
    def runDir = config.get('runDir', 'output/ci-run')

    if (!gameUrl) {
        error('playranger: gameUrl 必填')
    }

    return {
        stage('PlayRanger 试玩') {
            withEnv(['PLAYRANGER_CI=1']) {
                catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {
                    sh """
                        playranger test '${gameUrl}' \
                          --goal '${goal}' \
                          --max-steps ${maxSteps} \
                          --ci --perf \
                          --output ${runDir}
                    """
                }
            }
        }

        stage('缺陷门禁') {
            catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {
                sh """
                    python tools/ci_report.py \
                      --bugs ${runDir}/bugs \
                      --out-dir ${outDir} \
                      --fail-on '${failOn}' \
                      --allow ${allow}
                """
            }
        }

        stage('归档报告') {
            junit allowEmptyResults: true, testResults: "${outDir}/junit.xml"
            archiveArtifacts artifacts: "${outDir}/**", allowEmptyArchive: true
            archiveArtifacts artifacts: "${runDir}/bugs/**", allowEmptyArchive: true
        }
    }
}
