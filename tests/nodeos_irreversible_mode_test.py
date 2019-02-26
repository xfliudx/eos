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
   forkDbHeadBlockNum =  int(info["fork_db_head_block_num"])
   return headBlockNum, lib, forkDbHeadBlockNum

# List to contain the test result message
testResultMsgs = []
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

   def stopBlockProduction():
      if not producingNode.killed: producingNode.kill(signal.SIGTERM)

   def resumeBlockProduction():
      if producingNode.killed: producingNode.relaunch(producingNodeId, "", timeout=relaunchTimeout)

   # Wrapper function to execute test
   # This wrapper function will resurrect the node to be tested, and shut it down by the end of the test
   def executeTest(nodeIdOfNodeToTest, runTestScenario):
      try:
         nodeToTest = cluster.getNode(nodeIdOfNodeToTest)
         # Resurrect killed node to be tested
         isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout)
         assert isRelaunchSuccess, "Fail to relaunch before running test scenario"
         runTestScenario(nodeIdOfNodeToTest, nodeToTest)
         # Kill node after use
         if not nodeToTest.killed: nodeToTest.kill(signal.SIGTERM)
         testResultMsgs.append("!!!TEST CASE #{} IS SUCCESSFUL".format(nodeIdOfNodeToTest))
      except Exception as e:
         testResultMsgs.append("!!!BUG IS CONFIRMED ON TEST CASE #{} {}".format(nodeIdOfNodeToTest, e))

   # 1st test case: Replay in irreversible mode with reversible blocks
   # Bug: duplicate block added error
   def replayInIrrModeWithRevBlocks(nodeIdOfNodeToTest, nodeToTest):
      # Kill node and replay in irreversible mode
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible --replay", timeout=relaunchTimeout)
      assert isRelaunchSuccess, "Fail to relaunch"

   # 2nd test case: Replay in irreversible mode without reversible blocks
   # Bug: last_irreversible_block != the real lib (e.g. if lib is 1000, it replays up to 1000 saying head is 1000 and lib is 999)
   def replayInIrrModeWithoutRevBlocks(nodeIdOfNodeToTest, nodeToTest):
      # Track head block num and lib before shutdown
      headBlockNumBeforeShutdown, libBeforeShutdown, _ = getHeadBlockNumAndLib(nodeToTest)
      # Shut node, remove reversible blocks and relaunch
      nodeToTest.kill(signal.SIGTERM)
      removeReversibleBlocks(nodeIdOfNodeToTest)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--replay --read-mode irreversible", timeout=relaunchTimeout)
      assert isRelaunchSuccess, "Fail to relaunch"
      # Check head block num and lib
      headBlockNum, lib, _ = getHeadBlockNumAndLib(nodeToTest)

      assert_msg = "headBlockNumBeforeShutdown {}, headBlockNum {}, lib {}, libBeforeShutdown {}".format(
                   headBlockNumBeforeShutdown, headBlockNum, lib, libBeforeShutdown)
      assert (headBlockNum == libBeforeShutdown and lib == libBeforeShutdown), assert_msg

   # 3rd test case: Switch mode speculative -> irreversible -> speculative without replay and production disabled
   def switchBackForthSpecAndIrrMode(nodeIdOfNodeToTest, nodeToTest):
       # Track head block num and lib before shutdown
      headBlockNumBeforeShutdown, libBeforeShutdown, forkDbBlockNumBeforeShutdown = getHeadBlockNumAndLib(nodeToTest)
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)
      assert(isRelaunchSuccess)
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout, addOrSwapFlags={"--read-mode": "speculative"})
      assert(isRelaunchSuccess)
      # Check head block num and lib
      headBlockNum, lib, forkDbBlockNum = getHeadBlockNumAndLib(nodeToTest)
      Print("headBlockNumBeforeShutdown {}, headBlockNum {}, lib {}, libBeforeShutdown {}, forkDbBlockNum {}, forkDbBlockNumBeforeShutdown {}"
         .format(headBlockNumBeforeShutdown, headBlockNum, lib, libBeforeShutdown, forkDbBlockNum, forkDbBlockNumBeforeShutdown))
      assert_msg = "headBlockNumBeforeShutdown {}, headBlockNum {}, lib {}, libBeforeShutdown {}, forkDbBlockNum {}, forkDbBlockNumBeforeShutdown {}".format(
                   headBlockNumBeforeShutdown, headBlockNum, lib, libBeforeShutdown, forkDbBlockNum, forkDbBlockNumBeforeShutdown)
      assert (headBlockNum == libBeforeShutdown and lib == libBeforeShutdown and forkDbBlockNum == forkDbBlockNumBeforeShutdown), assert_msg


   # 4th test case: Switch mode speculative -> irreversible -> speculative without replay and production enabled
   def switchBackForthSpecAndIrrModeWithProdEnabled(nodeIdOfNodeToTest, nodeToTest):
      # Resume block production
      resumeBlockProduction()
      # Kill and relaunch in irreversible mode
      nodeToTest.kill(signal.SIGTERM)
      time.sleep(60) # Wait for some blocks to be produced and lib advance
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)
      assert isRelaunchSuccess, "Fail to relaunch"
      # Stop block production
      stopBlockProduction()

   # Start executing test cases here
   executeTest(1, replayInIrrModeWithRevBlocks)
   executeTest(2, replayInIrrModeWithoutRevBlocks)
   executeTest(3, switchBackForthSpecAndIrrMode)
   executeTest(4, switchBackForthSpecAndIrrModeWithProdEnabled)

finally:
   TestHelper.shutdown(cluster, walletMgr)
   # walletMgr.killall(allInstances=True)
   # Print test result
   for msg in testResultMsgs: Print(msg)

exit(0)
