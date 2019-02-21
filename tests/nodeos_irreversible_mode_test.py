#!/usr/bin/env python3

from testUtils import Utils
from Cluster import Cluster
from WalletMgr import WalletMgr
from Node import Node
from Node import ReturnType
from TestHelper import TestHelper
from testUtils import Account

import urllib.request
import re
import os
import time
import signal
import subprocess
import shutil


Print = Utils.Print
errorExit = Utils.errorExit
cmdError = Utils.cmdError
relaunchTimeout = 5

# Parse command line arguments
args = TestHelper.parse_args({"-v"})
Utils.Debug = args.v

# Setup cluster and it's wallet manager
walletMgr=WalletMgr(True)
cluster=Cluster(walletd=True)
cluster.setWalletMgr(walletMgr)

def getLatestStdErrFile(nodeId):
   dataDir = Cluster.getDataDir(nodeId)
   latestStdErrFile = None
   pattern = re.compile(r"stderr\..*\.txt")
   for item in os.listdir(dataDir):
      if pattern.match(item) and (latestStdErrFile is None or latestStdErrFile < item):
         latestStdErrFile = item
   if latestStdErrFile is None:
      latestStdErrFile = "stderr.txt"
   return os.path.join(dataDir, latestStdErrFile)

def checkStdErr(nodeId, message):
   latestStdErrFile = getLatestStdErrFile(nodeId)
   with open(latestStdErrFile, 'r') as f:
      for line in f:
         if message in line:
            return True
   return False

def removeReversibleBlocks(nodeId):
   dataDir = Cluster.getDataDir(nodeId)
   reversibleBlocks = os.path.join(dataDir, "blocks", "reversible")
   shutil.rmtree(reversibleBlocks, ignore_errors=True)

def getHeadBlockNumAndLib(node: Node):
   info = node.getInfo()
   headBlockNum = int(info["head_block_num"])
   lib = int(info["last_irreversible_block_num"])
   return headBlockNum, lib

try:
   TestHelper.printSystemInfo("BEGIN")
   cluster.killall(allInstances=True)
   cluster.cleanup()
   numOfProducers = 4
   totalNodes = 5
   cluster.launch(
      prodCount=numOfProducers,
      totalProducers=numOfProducers,
      totalNodes=totalNodes,
      pnodes=1,
      useBiosBootFile=False,
      topo="mesh",
      specificExtraNodeosArgs={0:"--plugin eosio::producer_api_plugin --enable-stale-production"},
      extraNodeosArgs=" --plugin eosio::net_api_plugin")

   # Give some time for it to produce, so lib is advancing
   producingNodeId = 0
   producingNode = cluster.getNode(producingNodeId)
   time.sleep(60)

   # Kill all nodes, so we can test all node in isolated environment
   for clusterNode in cluster.nodes:
      clusterNode.kill(signal.SIGTERM)
   cluster.biosNode.kill(signal.SIGTERM)

   def pauseBlockProduction():
      if not producingNode.killed: producingNode.kill(signal.SIGTERM)

   def resumeBlockProduction():
      if producingNode.killed: producingNode.relaunch(producingNodeId, "", timeout=relaunchTimeout)

   # Wrapper function to execute test
   # This wrapper function will resurrect the node to be tested, and shut it down by the end of the test
   def executeTest(nodeIdOfNodeToTest, runTestScenario):
      nodeToTest = cluster.getNode(nodeIdOfNodeToTest)
      # Resurrect killed node to be tested
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout)
      assert(isRelaunchSuccess)
      runTestScenario(nodeIdOfNodeToTest, nodeToTest)
      # Kill node after use
      if not nodeToTest.killed:
        nodeToTest.kill(signal.SIGTERM)

   # 1st test case: Replay in irreversible mode with reversible blocks
   # Bug: duplicate block added error
   def replayInIrrModeWithRevBlocks(nodeIdOfNodeToTest, nodeToTest):
      # Kill node and replay in irreversible mode
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible --replay", timeout=relaunchTimeout)

      isStdErrContainMessage = False
      if not isRelaunchSuccess:
         isStdErrContainMessage = checkStdErr(nodeIdOfNodeToTest, "duplicate block added")
      # TODO: Change this to proper assert
      if not isRelaunchSuccess and isStdErrContainMessage:
         Print("!!!BUG #1 CONFIRMED")
      else:
         Print("!!!BUG #1 IS NOT CONFIRMED")

   # 2nd test case: Replay in irreversible mode without reversible blocks
   # Bug: last_irreversible_block != the real lib (e.g. if lib is 1000, it replays up to 1000 saying head is 1000 and lib is 999)
   def replayInIrrModeWithoutRevBlocks(nodeIdOfNodeToTest, nodeToTest):
      # Track head block num and lib before shutdown
      headBlockNumBeforeShutdown, libBeforeShutdown = getHeadBlockNumAndLib(nodeToTest)
      # Shut node, remove reversible blocks and relaunch
      nodeToTest.kill(signal.SIGTERM)
      removeReversibleBlocks(nodeIdOfNodeToTest)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--replay --read-mode irreversible", timeout=relaunchTimeout)
      assert(isRelaunchSuccess)
      # Check head block num and lib
      headBlockNum, lib = getHeadBlockNumAndLib(nodeToTest)

      # TODO: Change this to proper assert
      Print("headBlockNumBeforeShutdown {}, headBlockNum {}, lib {}, libBeforeShutdown {}".format(headBlockNumBeforeShutdown, headBlockNum, lib, libBeforeShutdown))
      if not (headBlockNum == libBeforeShutdown and lib == libBeforeShutdown):
         Print("!!!BUG #2 CONFIRMED")
      else:
         Print("!!!BUG #2 IS NOT CONFIRMED")

   # 3rd test case: Switch mode speculative -> irreversible -> speculative without replay and production disabled
   def switchBackForthSpecAndIrrMode(nodeIdOfNodeToTest, nodeToTest):
      # 3-1 Switch mode speculative -> irreversible -> speculative while production is disabled
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)
      assert(isRelaunchSuccess)
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout, addOrSwapFlags={"--read-mode": "speculative"})
      assert(isRelaunchSuccess)
      Print("3RD TEST CASE SUCCESS")

   # 4th test case: Switch mode speculative -> irreversible -> speculative without replay and production enabled
   def switchBackForthSpecAndIrrModeWithProdEnabled(nodeIdOfNodeToTest, nodeToTest):
      # Resume block production
      resumeBlockProduction()

      # Kill and relaunch in irreversible mode
      nodeToTest.kill(signal.SIGTERM)
      time.sleep(60) # Wait for some blocks to be produced and lib advance
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)

      # Current code will throw a block_validate_exception "next block must be in the future"
      isStdErrContainMessage = False
      if not isRelaunchSuccess:
         isStdErrContainMessage = checkStdErr(nodeIdOfNodeToTest, "next block must be in the future")
      # TODO: Change this to proper assert
      if not isRelaunchSuccess and isStdErrContainMessage:
         Print("!!!BUG #4-1 CONFIRMED")
      else:
         Print("!!!BUG #4-1 IS NOT CONFIRMED")

      # After it fails to relaunch, if we try to relaunch again, it will complain unlinkable block
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)
      isStdErrContainMessage = False
      if not isRelaunchSuccess:
         isStdErrContainMessage = checkStdErr(nodeIdOfNodeToTest, "unlinkable block")
      # TODO: Change this to proper assert
      if not isRelaunchSuccess and isStdErrContainMessage:
         Print("!!!BUG #4-2 CONFIRMED")
      else:
         Print("!!!BUG #4-2 IS NOT CONFIRMED")

      # Stop block production
      pauseBlockProduction()

   executeTest(1, replayInIrrModeWithRevBlocks)
   executeTest(2, replayInIrrModeWithoutRevBlocks)
   executeTest(3, switchBackForthSpecAndIrrMode)
   executeTest(4, switchBackForthSpecAndIrrModeWithProdEnabled)


finally:
   TestHelper.shutdown(cluster, walletMgr)
   # walletMgr.killall(allInstances=True)
   print("finally")

exit(0)
